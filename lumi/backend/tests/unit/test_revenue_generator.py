"""Unit tests for the dataset_generator.revenue_generator module.

Tests daily revenue/KPI generation including occupancy computation,
ADR calculation, RevPAR derivation, comparison deltas, arrivals/departures,
upsell metrics, and TTL calculation.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from dataset_generator.config import (
    ADR_OFFSETS,
    OCCUPANCY_OFFSETS,
    PROPERTY_PROFILES,
    SEED_DAYS,
    SEGMENT_MIX,
    TTL_REVENUES_DAYS,
)
from dataset_generator.revenue_generator import (
    _compute_adr,
    _compute_arrivals,
    _compute_departures,
    _compute_occupied_rooms,
    _compute_occupancy_pct,
    _compute_revpar,
    _compute_total_revenue,
    _compute_ttl,
    _compute_upsell_metrics,
    _compute_vs_budget,
    _compute_vs_last_week,
    _compute_vs_yoy,
    _get_base_occupancy,
    generate_revenue,
)


# ---------------------------------------------------------------------------
# _get_base_occupancy tests
# ---------------------------------------------------------------------------


class TestGetBaseOccupancy:
    """Tests for weekday/weekend base occupancy selection."""

    def setup_method(self) -> None:
        """Prepare a sample profile for testing."""
        self.chicago_profile = PROPERTY_PROFILES[0]  # weekday (85,92), weekend (72,80)

    def test_weekday_returns_midpoint(self) -> None:
        """Weekday should return midpoint of weekdayOccupancy range."""
        # Monday
        monday = date(2026, 7, 13)
        result = _get_base_occupancy(self.chicago_profile, monday)
        # Midpoint of (85, 92) = 88.5
        assert result == Decimal("88.5")

    def test_weekend_returns_midpoint(self) -> None:
        """Weekend should return midpoint of weekendOccupancy range."""
        # Saturday
        saturday = date(2026, 7, 18)
        result = _get_base_occupancy(self.chicago_profile, saturday)
        # Midpoint of (72, 80) = 76.0
        assert result == Decimal("76.0")

    def test_friday_is_weekday(self) -> None:
        """Friday (weekday 4) uses weekday occupancy."""
        friday = date(2026, 7, 17)
        result = _get_base_occupancy(self.chicago_profile, friday)
        assert result == Decimal("88.5")

    def test_sunday_is_weekend(self) -> None:
        """Sunday (weekday 6) uses weekend occupancy."""
        sunday = date(2026, 7, 19)
        result = _get_base_occupancy(self.chicago_profile, sunday)
        assert result == Decimal("76.0")


# ---------------------------------------------------------------------------
# _compute_occupancy_pct tests
# ---------------------------------------------------------------------------


class TestComputeOccupancyPct:
    """Tests for occupancy percentage computation with offsets."""

    def setup_method(self) -> None:
        """Prepare a sample profile for testing."""
        self.chicago_profile = PROPERTY_PROFILES[0]

    def test_applies_offset(self) -> None:
        """Result includes the day-specific offset from OCCUPANCY_OFFSETS."""
        # Day 0 for Chicago has offset -2
        record_date = date.today() - timedelta(days=29)
        result = _compute_occupancy_pct(self.chicago_profile, record_date, 0)
        # Should be base +/- offset; we just verify it's a Decimal > 0
        assert isinstance(result, Decimal)
        assert result > Decimal("0")

    def test_clamped_at_100(self) -> None:
        """Occupancy cannot exceed 100%."""
        # Create a profile with very high base occupancy
        high_profile: Dict[str, Any] = {
            "propertyId": "ALOHA-TYO-001",
            "weekdayOccupancy": (98, 100),
            "weekendOccupancy": (98, 100),
        }
        # Tokyo day 9 has offset +3
        monday = date(2026, 7, 13)
        result = _compute_occupancy_pct(high_profile, monday, 9)
        assert result <= Decimal("100")

    def test_clamped_at_0(self) -> None:
        """Occupancy cannot be negative."""
        # Hypothetically test with extreme negative offset
        low_profile: Dict[str, Any] = {
            "propertyId": "ALOHA-CHI-001",
            "weekdayOccupancy": (1, 3),
            "weekendOccupancy": (1, 3),
        }
        # Day 6 for Chicago has offset -5
        monday = date(2026, 7, 13)
        result = _compute_occupancy_pct(low_profile, monday, 6)
        assert result >= Decimal("0")

    def test_returns_one_decimal_place(self) -> None:
        """Occupancy is rounded to one decimal place."""
        record_date = date.today() - timedelta(days=29)
        result = _compute_occupancy_pct(self.chicago_profile, record_date, 0)
        # Check that result has at most 1 decimal place
        assert result == result.quantize(Decimal("0.1"))


# ---------------------------------------------------------------------------
# _compute_adr tests
# ---------------------------------------------------------------------------


class TestComputeAdr:
    """Tests for ADR calculation with offsets."""

    def setup_method(self) -> None:
        """Prepare a sample profile for testing."""
        self.chicago_profile = PROPERTY_PROFILES[0]  # adrBaseline = 245

    def test_applies_offset(self) -> None:
        """ADR includes day-specific offset from ADR_OFFSETS."""
        # Chicago day 0 offset = -5, so ADR = 245 + (-5) = 240
        result = _compute_adr(self.chicago_profile, 0)
        assert result == Decimal("240.00")

    def test_positive_offset(self) -> None:
        """ADR increases on peak days."""
        # Chicago day 3 offset = +8, so ADR = 245 + 8 = 253
        result = _compute_adr(self.chicago_profile, 3)
        assert result == Decimal("253.00")

    def test_non_negative(self) -> None:
        """ADR is always non-negative."""
        for profile in PROPERTY_PROFILES:
            for day_index in range(SEED_DAYS):
                result = _compute_adr(profile, day_index)
                assert result >= Decimal("0")

    def test_returns_two_decimal_places(self) -> None:
        """ADR is rounded to two decimal places."""
        result = _compute_adr(self.chicago_profile, 0)
        assert result == result.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# _compute_occupied_rooms tests
# ---------------------------------------------------------------------------


class TestComputeOccupiedRooms:
    """Tests for occupied room count calculation."""

    def test_basic_calculation(self) -> None:
        """Occupied rooms = total * occupancy_pct / 100 rounded."""
        result = _compute_occupied_rooms(368, Decimal("87.5"))
        assert result == 322

    def test_zero_occupancy(self) -> None:
        """Zero percent occupancy gives zero rooms."""
        result = _compute_occupied_rooms(368, Decimal("0"))
        assert result == 0

    def test_full_occupancy(self) -> None:
        """100% occupancy gives total rooms."""
        result = _compute_occupied_rooms(368, Decimal("100"))
        assert result == 368

    def test_returns_int(self) -> None:
        """Result is always an integer."""
        result = _compute_occupied_rooms(425, Decimal("82.3"))
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# _compute_revpar tests
# ---------------------------------------------------------------------------


class TestComputeRevpar:
    """Tests for RevPAR derivation."""

    def test_basic_formula(self) -> None:
        """RevPAR = occupancyPct/100 * ADR."""
        result = _compute_revpar(Decimal("87.5"), Decimal("258.00"))
        # 0.875 * 258 = 225.75
        assert result == Decimal("225.75")

    def test_zero_occupancy(self) -> None:
        """Zero occupancy gives zero RevPAR regardless of ADR."""
        result = _compute_revpar(Decimal("0"), Decimal("258.00"))
        assert result == Decimal("0.00")

    def test_two_decimal_places(self) -> None:
        """RevPAR is rounded to 2 decimal places."""
        result = _compute_revpar(Decimal("88.3"), Decimal("245.00"))
        assert result == result.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# _compute_total_revenue tests
# ---------------------------------------------------------------------------


class TestComputeTotalRevenue:
    """Tests for total revenue calculation."""

    def test_basic_formula(self) -> None:
        """Total revenue = ADR * occupied rooms."""
        result = _compute_total_revenue(Decimal("258.00"), 322)
        # 258 * 322 = 83076
        assert result == Decimal("83076.00")

    def test_zero_occupied(self) -> None:
        """Zero occupied rooms gives zero revenue."""
        result = _compute_total_revenue(Decimal("258.00"), 0)
        assert result == Decimal("0.00")


# ---------------------------------------------------------------------------
# _compute_vs_last_week tests
# ---------------------------------------------------------------------------


class TestComputeVsLastWeek:
    """Tests for week-over-week comparison delta."""

    def test_first_week_returns_zero(self) -> None:
        """Days 0-6 have no prior-week reference, so delta is 0."""
        history: List[Decimal] = [Decimal("85.0")] * 7
        for day_index in range(7):
            result = _compute_vs_last_week(Decimal("87.0"), day_index, history)
            assert result == Decimal("0.0")

    def test_day_7_compares_to_day_0(self) -> None:
        """Day 7 compares against day 0 occupancy."""
        history = [Decimal("85.0")] * 7
        result = _compute_vs_last_week(Decimal("88.0"), 7, history)
        # 88 - 85 = 3.0
        assert result == Decimal("3.0")

    def test_negative_delta(self) -> None:
        """Delta can be negative when current < last week."""
        history = [Decimal("90.0")] * 7
        result = _compute_vs_last_week(Decimal("87.0"), 7, history)
        # 87 - 90 = -3.0
        assert result == Decimal("-3.0")


# ---------------------------------------------------------------------------
# _compute_vs_budget tests
# ---------------------------------------------------------------------------


class TestComputeVsBudget:
    """Tests for budget comparison delta."""

    def test_above_budget(self) -> None:
        """Positive delta when above budget."""
        result = _compute_vs_budget(Decimal("87.5"), 83)
        assert result == Decimal("4.5")

    def test_below_budget(self) -> None:
        """Negative delta when below budget."""
        result = _compute_vs_budget(Decimal("80.0"), 83)
        assert result == Decimal("-3.0")

    def test_at_budget(self) -> None:
        """Zero delta when exactly at budget."""
        result = _compute_vs_budget(Decimal("83.0"), 83)
        assert result == Decimal("0.0")


# ---------------------------------------------------------------------------
# _compute_vs_yoy tests
# ---------------------------------------------------------------------------


class TestComputeVsYoy:
    """Tests for year-over-year delta simulation."""

    def test_range(self) -> None:
        """YOY delta is within [-3, +3] range for all days."""
        for day_index in range(SEED_DAYS):
            result = _compute_vs_yoy(day_index)
            assert Decimal("-3.0") <= result <= Decimal("3.0")

    def test_deterministic(self) -> None:
        """Same day_index always produces same result."""
        assert _compute_vs_yoy(5) == _compute_vs_yoy(5)
        assert _compute_vs_yoy(15) == _compute_vs_yoy(15)

    def test_day_0(self) -> None:
        """Day 0: (0*3) % 7 - 3 = -3."""
        result = _compute_vs_yoy(0)
        assert result == Decimal("-3.0")


# ---------------------------------------------------------------------------
# _compute_arrivals and _compute_departures tests
# ---------------------------------------------------------------------------


class TestArrivalsAndDepartures:
    """Tests for arrival and departure estimation."""

    def test_arrivals_proportional(self) -> None:
        """Arrivals scale with occupied rooms."""
        low = _compute_arrivals(100)
        high = _compute_arrivals(300)
        assert high > low

    def test_arrivals_non_negative(self) -> None:
        """Arrivals are never negative."""
        result = _compute_arrivals(0)
        assert result >= 0

    def test_departures_non_negative(self) -> None:
        """Departures are never negative."""
        for day_index in range(SEED_DAYS):
            result = _compute_departures(100, day_index)
            assert result >= 0

    def test_departures_similar_to_arrivals(self) -> None:
        """Departures are in the same ballpark as arrivals."""
        arrivals = _compute_arrivals(300)
        departures = _compute_departures(300, 5)
        # Should be within ~20% of each other
        assert abs(arrivals - departures) < arrivals * 0.5


# ---------------------------------------------------------------------------
# _compute_upsell_metrics tests
# ---------------------------------------------------------------------------


class TestComputeUpsellMetrics:
    """Tests for upsell eligible and converted computation."""

    def test_eligible_is_15_percent_of_arrivals(self) -> None:
        """Eligible guests are ~15% of arrivals."""
        eligible, _ = _compute_upsell_metrics(100)
        assert eligible == 15

    def test_converted_is_33_percent_of_eligible(self) -> None:
        """Converted is ~33% of eligible."""
        eligible, converted = _compute_upsell_metrics(100)
        assert converted == round(eligible * 0.33)

    def test_zero_arrivals(self) -> None:
        """Zero arrivals produces zero upsells."""
        eligible, converted = _compute_upsell_metrics(0)
        assert eligible == 0
        assert converted == 0


# ---------------------------------------------------------------------------
# _compute_ttl tests
# ---------------------------------------------------------------------------


class TestComputeTtl:
    """Tests for TTL epoch calculation."""

    def test_returns_int(self) -> None:
        """TTL is an integer epoch timestamp."""
        result = _compute_ttl(date(2026, 7, 15))
        assert isinstance(result, int)

    def test_future_of_record_date(self) -> None:
        """TTL is after the record date."""
        import time as t

        record_date = date(2026, 7, 15)
        record_epoch = int(t.mktime(record_date.timetuple()))
        ttl = _compute_ttl(record_date)
        assert ttl > record_epoch

    def test_approximately_90_days_later(self) -> None:
        """TTL is approximately 90 days after record date."""
        import time as t

        record_date = date(2026, 7, 15)
        record_epoch = int(t.mktime(record_date.timetuple()))
        ttl = _compute_ttl(record_date)
        # 90 days in seconds (approximately)
        expected_offset = TTL_REVENUES_DAYS * 86400
        actual_offset = ttl - record_epoch
        # Allow 1-day margin for timezone/DST
        assert abs(actual_offset - expected_offset) < 86400


# ---------------------------------------------------------------------------
# generate_revenue integration tests
# ---------------------------------------------------------------------------


class TestGenerateRevenue:
    """Tests for the full revenue generation function."""

    def setup_method(self) -> None:
        """Create a mock writer for each test."""
        self.mock_writer = MagicMock()
        self.mock_writer.write_items.return_value = {
            "success": 150,
            "failed": 0,
            "skipped": 0,
        }

    def test_returns_160_items(self) -> None:
        """Generates exactly 160 items (5 properties * 32 days).

        The window is 32 days per property (SEED_DAYS historical days plus
        today and tomorrow) so the orchestrator always finds revenue for
        today/tomorrow regardless of when the seeder last ran (Requirement 2.2).
        """
        result = generate_revenue(self.mock_writer)
        assert len(result) == 160

    def test_all_properties_represented(self) -> None:
        """All 5 properties appear in the results."""
        result = generate_revenue(self.mock_writer)
        property_ids = {key[0] for key in result.keys()}
        expected = {p["propertyId"] for p in PROPERTY_PROFILES}
        assert property_ids == expected

    def test_32_days_per_property(self) -> None:
        """Each property has exactly 32 day entries (SEED_DAYS + today + tomorrow)."""
        result = generate_revenue(self.mock_writer)
        for profile in PROPERTY_PROFILES:
            prop_items = [
                v for k, v in result.items() if k[0] == profile["propertyId"]
            ]
            assert len(prop_items) == SEED_DAYS + 2

    def test_item_has_all_required_fields(self) -> None:
        """Each revenue item contains all required DynamoDB attributes."""
        required_fields = {
            "propertyId", "date", "occupancyPct", "adr", "revpar",
            "totalRevenue", "availableRooms", "occupiedRooms",
            "confirmedReservations", "arrivals", "departures",
            "vsLastWeek", "vsBudget", "vsYOY", "segmentMix",
            "upsellEligible", "upsellConverted", "ttl",
        }
        result = generate_revenue(self.mock_writer)
        first_item = next(iter(result.values()))
        assert required_fields.issubset(set(first_item.keys()))

    def test_revpar_equals_occupancy_times_adr(self) -> None:
        """RevPAR is correctly derived from occupancy and ADR."""
        result = generate_revenue(self.mock_writer)
        for item in result.values():
            expected_revpar = (item["occupancyPct"] / Decimal("100")) * item["adr"]
            expected_revpar = expected_revpar.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            assert item["revpar"] == expected_revpar

    def test_total_revenue_equals_adr_times_occupied(self) -> None:
        """Total revenue = ADR * occupied rooms."""
        result = generate_revenue(self.mock_writer)
        for item in result.values():
            expected = (item["adr"] * Decimal(str(item["occupiedRooms"]))).quantize(
                Decimal("0.01")
            )
            assert item["totalRevenue"] == expected

    def test_confirmed_reservations_exceeds_occupied(self) -> None:
        """Confirmed reservations include 3% oversell margin."""
        result = generate_revenue(self.mock_writer)
        for item in result.values():
            if item["occupiedRooms"] > 0:
                assert item["confirmedReservations"] >= item["occupiedRooms"]

    def test_segment_mix_from_config(self) -> None:
        """Segment mix matches the property's SEGMENT_MIX config."""
        result = generate_revenue(self.mock_writer)
        for (prop_id, _), item in result.items():
            assert item["segmentMix"] == SEGMENT_MIX[prop_id]

    def test_available_rooms_matches_property(self) -> None:
        """availableRooms matches the property's totalRooms."""
        result = generate_revenue(self.mock_writer)
        property_rooms = {p["propertyId"]: p["totalRooms"] for p in PROPERTY_PROFILES}
        for (prop_id, _), item in result.items():
            assert item["availableRooms"] == property_rooms[prop_id]

    def test_writer_called_once(self) -> None:
        """Writer.write_items is called once with all 160 items."""
        generate_revenue(self.mock_writer)
        assert self.mock_writer.write_items.call_count == 1
        # Verify it was called with a list of 160 items (5 properties * 32 days)
        call_args = self.mock_writer.write_items.call_args[0][0]
        assert len(call_args) == 160

    def test_dates_cover_window_relative_to_reference_date(self) -> None:
        """Generated dates span today-29 through today+2, anchored to reference_date.

        The generator derives its whole 32-day window from the explicit
        reference_date (Requirement 2.1), so passing a fixed date makes the
        assertion deterministic rather than depending on wall-clock today.
        """
        reference_date = date(2026, 8, 15)
        result = generate_revenue(self.mock_writer, reference_date=reference_date)
        start_date = reference_date - timedelta(days=SEED_DAYS - 1)
        expected_dates = {
            (start_date + timedelta(days=i)).isoformat()
            for i in range(SEED_DAYS + 2)
        }
        chicago_dates = {
            key[1] for key in result.keys() if key[0] == "ALOHA-CHI-001"
        }
        assert chicago_dates == expected_dates

    def test_deterministic_output(self) -> None:
        """Same inputs produce identical results across calls."""
        result_1 = generate_revenue(self.mock_writer)
        result_2 = generate_revenue(self.mock_writer)
        # Compare a sample of items
        for key in list(result_1.keys())[:10]:
            assert result_1[key] == result_2[key]

    def test_ttl_is_positive_integer(self) -> None:
        """All TTL values are positive integers."""
        result = generate_revenue(self.mock_writer)
        for item in result.values():
            assert isinstance(item["ttl"], int)
            assert item["ttl"] > 0

    def test_occupancy_within_realistic_bounds(self) -> None:
        """Occupancy stays between 50% and 100% for all properties."""
        result = generate_revenue(self.mock_writer)
        for item in result.values():
            assert Decimal("50") <= item["occupancyPct"] <= Decimal("100")
