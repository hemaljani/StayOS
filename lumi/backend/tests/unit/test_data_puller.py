"""Unit tests for the LUMI data puller module.

Tests mock mode data retrieval, DynamoDB dataset mode with graceful
degradation, and the action prioritizer sorting logic.
"""

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from action_prioritizer import prioritize_actions
from data_puller import (
    MAX_VIP_ALERTS,
    MAX_VIP_ARRIVALS,
    _build_vip_arrival_entry,
    _derive_action_items,
    _format_kpis,
    _get_mock_data,
    _merge_source_data,
    _pull_live_data,
    _query_vip_arrivals,
    pull_property_data,
)
from orchestrator_exceptions import AllSourcesFailedError, DataPullError


# -- Test fixtures --


@pytest.fixture()
def sample_settings() -> dict:
    """Provide sample GM settings for testing."""
    return {
        "gmAlias": "jsmith",
        "gmName": "Jennifer Smith",
        "propertyName": "Aloha Grand Chicago",
        "audioPreferences": {
            "language": "en-US",
            "briefLength": "standard",
        },
    }


@pytest.fixture()
def sample_property_id() -> str:
    """Provide a sample property ID for testing."""
    return "ALOHA-CHI-001"


def _build_client_error(operation_name: str = "Query") -> ClientError:
    """Create a realistic DynamoDB service error for degradation tests.

    Args:
        operation_name: AWS operation reported in the error.

    Returns:
        ClientError matching an internal DynamoDB failure.
    """
    return ClientError(
        {
            "Error": {
                "Code": "InternalServerError",
                "Message": "DB error",
            }
        },
        operation_name,
    )


def _make_vip(
    guest_id: str,
    loyalty_tier: str = "AMBASSADOR",
    total_stays: int = 50,
    room_type: str = "KING_STANDARD",
    special_occasion: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a complete VIP payload for focused business-logic tests.

    Args:
        guest_id: Stable guest identifier.
        loyalty_tier: Loyalty tier for the candidate.
        total_stays: Lifetime stay count.
        room_type: Assigned reservation room type.
        special_occasion: Optional profile occasion.

    Returns:
        VIP arrival dict matching the frontend schema used by the data puller.
    """
    return {
        "guestId": guest_id,
        "guestName": f"Guest {guest_id}",
        "loyaltyTier": loyalty_tier,
        "totalStays": total_stays,
        "roomNumber": guest_id[-3:],
        "roomType": room_type,
        "specialOccasion": special_occasion,
        "preferences": ["HIGH_FLOOR"],
    }


# -- Tests for pull_property_data (mock mode) --


class TestPullPropertyDataMockMode:
    """Tests for mock mode data retrieval."""

    def test_mock_mode_returns_complete_data(
        self, sample_property_id: str, sample_settings: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mock mode returns all expected top-level keys."""
        monkeypatch.setenv("MOCK_MODE", "true")

        # Need to reimport to pick up env var change
        with patch("data_puller.MOCK_MODE", True):
            result = pull_property_data(sample_property_id, sample_settings)

        assert "property" in result
        assert "dailyKPIs" in result
        assert "actionItems" in result
        assert "vipArrivals" in result
        assert "dataSourceStatus" in result

    def test_mock_mode_property_has_correct_id(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """Mock data uses the provided property ID."""
        with patch("data_puller.MOCK_MODE", True):
            result = pull_property_data(sample_property_id, sample_settings)

        assert result["property"]["propertyId"] == sample_property_id

    def test_mock_mode_uses_settings_gm_name(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """Mock data uses GM name from settings."""
        with patch("data_puller.MOCK_MODE", True):
            result = pull_property_data(sample_property_id, sample_settings)

        assert result["property"]["gmName"] == "Jennifer Smith"

    def test_mock_mode_kpis_have_occupancy(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """Mock KPIs include occupancy data with expected fields."""
        with patch("data_puller.MOCK_MODE", True):
            result = pull_property_data(sample_property_id, sample_settings)

        occupancy = result["dailyKPIs"]["occupancy"]
        assert "current" in occupancy
        assert "vsLastWeek" in occupancy
        assert "forecast3pm" in occupancy
        assert occupancy["current"] == 87

    def test_mock_mode_action_items_sorted_by_severity(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """Mock action items include multiple severity levels."""
        with patch("data_puller.MOCK_MODE", True):
            result = pull_property_data(sample_property_id, sample_settings)

        action_items = result["actionItems"]
        assert len(action_items) == 5
        # First two should be URGENT
        assert action_items[0]["severity"] == "URGENT"
        assert action_items[1]["severity"] == "URGENT"

    def test_mock_mode_vip_arrivals_complete(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """Mock VIP arrivals include all 7 guests with required fields."""
        with patch("data_puller.MOCK_MODE", True):
            result = pull_property_data(sample_property_id, sample_settings)

        vips = result["vipArrivals"]
        assert len(vips) == 7

        # Check required fields on first VIP
        first_vip = vips[0]
        assert "guestId" in first_vip
        assert "guestName" in first_vip
        assert "loyaltyTier" in first_vip
        assert "roomNumber" in first_vip
        assert "preferences" in first_vip

    def test_mock_mode_data_source_status_all_mock(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """All data sources are marked as MOCK in mock mode."""
        with patch("data_puller.MOCK_MODE", True):
            result = pull_property_data(sample_property_id, sample_settings)

        status = result["dataSourceStatus"]
        assert all(value == "MOCK" for value in status.values())
        assert len(status) == 5


# -- Tests for dataset mode (graceful degradation) --


class TestPullDatasetDataDegradation:
    """Tests for graceful degradation when dataset queries fail."""

    def test_all_queries_fail_raises_error(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """AllSourcesFailedError raised when every dataset query fails."""
        with patch("data_puller.MOCK_MODE", False):
            # Patch all query functions to raise exceptions
            with patch("data_puller._query_revenue", side_effect=_build_client_error()):
                with patch("data_puller._query_arrivals", side_effect=_build_client_error()):
                    with patch("data_puller._query_vip_arrivals", side_effect=_build_client_error()):
                        with patch("data_puller._query_ooo_rooms", side_effect=_build_client_error()):
                            with patch("data_puller._query_open_work_orders", side_effect=_build_client_error()):
                                with pytest.raises(AllSourcesFailedError):
                                    pull_property_data(sample_property_id, sample_settings)

    def test_partial_failure_returns_available_data(
        self, sample_property_id: str, sample_settings: dict
    ) -> None:
        """Partial data returned when some dataset queries succeed."""
        mock_revenue = {
            "propertyId": sample_property_id,
            "date": "2025-01-15",
            "occupancyPct": 85,
            "adr": 248,
            "revpar": 211,
            "confirmedReservations": 320,
            "availableRooms": 368,
            "totalArrivals": 120,
            "totalDepartures": 100,
            "currency": "USD",
        }

        with patch("data_puller.MOCK_MODE", False):
            with patch("data_puller._query_revenue", return_value=mock_revenue):
                with patch("data_puller._query_arrivals", side_effect=_build_client_error()):
                    with patch("data_puller._query_ooo_rooms", return_value=[]):
                        with patch("data_puller._query_open_work_orders", return_value=[]):
                            result = pull_property_data(sample_property_id, sample_settings)

        # Revenue succeeded, arrivals failed, rooms/work-orders succeeded
        assert result["dataSourceStatus"]["REVENUE"] == "SUCCESS"
        assert result["dataSourceStatus"]["RESERVATIONS"] == "FAILED"
        assert result["dataSourceStatus"]["ROOMS"] == "SUCCESS"
        assert result["dataSourceStatus"]["WORK_ORDERS"] == "SUCCESS"
        # KPIs still populated from revenue
        assert result["dailyKPIs"]["occupancy"]["current"] == 85


class TestDatasetUiRegressionFixes:
    """Targeted regression coverage for dataset-backed UI payloads."""

    def test_vip_query_deduplicates_caps_and_ranks_premium_rooms(
        self, sample_property_id: str
    ) -> None:
        """VIP curation enriches before ranking and returns unique premium mix.

        Args:
            sample_property_id: Property identifier fixture.
        """
        room_types = [
            "KING_STANDARD",
            "KING_STANDARD",
            "KING_DELUXE",
            "QUEEN_DELUXE",
            "SUITE",
            "KING_STANDARD",
            "PENTHOUSE",
            "KING_DELUXE",
            "KING_STANDARD",
        ]
        arrivals: List[Dict[str, Any]] = []
        profile_responses: List[Dict[str, Any]] = []
        for guest_number, room_type in enumerate(room_types, start=1):
            guest_id = f"GUEST-{guest_number:03d}"
            arrivals.append({
                "guestId": guest_id,
                "guestName": f"Guest {guest_number}",
                "loyaltyTier": "AMBASSADOR" if guest_number % 2 else "TITANIUM",
                "roomNumber": str(100 + guest_number),
                "roomType": room_type,
            })
            profile_responses.append({
                "Item": {
                    "guestId": guest_id,
                    "totalStays": 20 + guest_number * 5,
                    "specialOccasion": "ANNIVERSARY" if guest_number == 1 else None,
                    "preferences": [],
                }
            })

        # A duplicate reservation must not consume a display slot or profile read.
        arrivals.append(dict(arrivals[0]))
        guests_table = MagicMock()
        guests_table.get_item.side_effect = profile_responses

        with patch("data_puller._dynamodb_resource.Table", return_value=guests_table):
            curated = _query_vip_arrivals(
                sample_property_id,
                "2026-08-03",
                arrivals,
            )

        guest_ids = [vip["guestId"] for vip in curated]
        room_priorities = {
            "PENTHOUSE": 0,
            "SUITE": 1,
            "KING_DELUXE": 2,
            "QUEEN_DELUXE": 3,
            "KING_STANDARD": 4,
        }
        assert len(curated) <= MAX_VIP_ARRIVALS
        assert len(guest_ids) == len(set(guest_ids))
        assert curated[0]["roomType"] == "PENTHOUSE"
        assert [room_priorities[vip["roomType"]] for vip in curated] == sorted(
            room_priorities[vip["roomType"]] for vip in curated
        )

    def test_vip_action_cards_are_bounded_unique_and_notable_only(self) -> None:
        """At most three unique Ambassador/Titanium notable alerts are emitted."""
        vip_arrivals = [
            _make_vip("GUEST-001", total_stays=55),
            _make_vip("GUEST-001", total_stays=55),
            _make_vip("GUEST-002", loyalty_tier="TITANIUM", special_occasion="BIRTHDAY"),
            _make_vip("GUEST-003", total_stays=41),
            _make_vip("GUEST-004", total_stays=75),
            _make_vip("GUEST-005", total_stays=40),
            _make_vip("GUEST-006", loyalty_tier="PLATINUM", total_stays=90),
        ]

        action_items = _derive_action_items(
            revenue=None,
            arrivals=[],
            ooo_rooms=[],
            work_orders=[],
            vip_arrivals=vip_arrivals,
            settings={"alertToggles": {"vipArrivalAlert": True}},
        )
        vip_actions = [
            item for item in action_items if item["type"] == "VIP_ARRIVAL_ALERT"
        ]
        guest_ids = [item["data"]["guestId"] for item in vip_actions]

        assert len(vip_actions) <= MAX_VIP_ALERTS
        assert len(guest_ids) == len(set(guest_ids))
        assert "GUEST-005" not in guest_ids
        assert "GUEST-006" not in guest_ids

    def test_ooo_issue_joins_work_order_and_uses_premium_room_flag(self) -> None:
        """OOO cards resolve issue type and premium state from actual fields."""
        action_items = _derive_action_items(
            revenue=None,
            arrivals=[],
            ooo_rooms=[{
                "roomNumber": "2401",
                "status": "OOO",
                "currentWorkOrderId": "WO-123",
                "isPremiumRoom": True,
                "view": "LAKE",
            }],
            work_orders=[{
                "workOrderId": "WO-123",
                "issueType": "HVAC",
            }],
            vip_arrivals=[],
            settings={"alertToggles": {"roomsOutOfOrder": True}},
        )

        room = action_items[0]["data"]["rooms"][0]
        assert room["issue"] == "HVAC"
        assert room["issue"] != "Unknown"
        assert room["isPremium"] is True

    def test_kpis_map_actual_fields_and_use_curated_unique_vips(self) -> None:
        """Revenue deltas and curated VIP counts populate frontend KPI fields."""
        revenue = {
            "occupancyPct": 87,
            "adr": 248,
            "revpar": 216,
            "vsLastWeek": 4.2,
            "vsBudget": 2.1,
            "vsYOY": 7.1,
            "arrivals": 142,
            "departures": 118,
            "confirmedReservations": 374,
            "availableRooms": 368,
        }
        reservation_rows = [
            {"guestId": f"ROW-{row_number}", "loyaltyTier": "AMBASSADOR"}
            for row_number in range(115)
        ]
        curated_vips = [
            _make_vip("GUEST-001", loyalty_tier="AMBASSADOR"),
            _make_vip("GUEST-002", loyalty_tier="TITANIUM"),
            _make_vip("GUEST-002", loyalty_tier="TITANIUM"),
            _make_vip("GUEST-003", loyalty_tier="PLATINUM"),
        ]

        kpis = _format_kpis(
            revenue,
            reservation_rows,
            curated_vips,
            "2026-08-03",
        )
        vip_summary = kpis["arrivals"]

        assert kpis["occupancy"]["vsLastWeek"] == 4.2
        assert kpis["occupancy"]["vsBudget"] == 2.1
        assert kpis["adr"]["vsLastWeek"] == 4.2
        assert kpis["adr"]["vsBudget"] == 2.1
        assert kpis["revPAR"]["vsYOY"] == 7.1
        assert kpis["arrivals"]["total"] == 142
        assert kpis["departures"]["total"] == 118
        assert vip_summary["vipCount"] == 3
        assert (
            vip_summary["ambassadorCount"]
            + vip_summary["titaniumCount"]
            + vip_summary["platinumCount"]
            == vip_summary["vipCount"]
        )

    def test_estimated_arrival_times_are_deterministic_and_varied(self) -> None:
        """Generated VIP arrival times vary and remain within 13:00-22:59."""
        estimated_arrivals = [
            _build_vip_arrival_entry(
                {
                    "guestId": f"GUEST-{guest_number:03d}",
                    "guestName": f"Guest {guest_number}",
                    "loyaltyTier": "AMBASSADOR",
                    "roomNumber": str(100 + guest_number),
                    "roomType": "KING_STANDARD",
                },
                {"totalStays": 50},
                "2026-08-03",
            )["estimatedArrival"]
            for guest_number in range(1, 8)
        ]
        arrival_hours = [int(arrival[11:13]) for arrival in estimated_arrivals]

        assert len(set(estimated_arrivals)) > 1
        assert any(not arrival.endswith("T14:00:00") for arrival in estimated_arrivals)
        assert all(13 <= arrival_hour <= 22 for arrival_hour in arrival_hours)


# -- Tests for _merge_source_data --


class TestMergeSourceData:
    """Tests for source data merging logic."""

    def test_merge_xpms_data(self) -> None:
        """xPMS data merges into property, dailyKPIs, and actionItems."""
        combined: dict = {
            "property": {},
            "dailyKPIs": {},
            "actionItems": [],
            "vipArrivals": [],
        }
        source_data = {
            "property": {"propertyId": "TEST-001"},
            "dailyKPIs": {"occupancy": {"current": 90}},
            "actionItems": [{"id": "a1", "severity": "HIGH"}],
        }

        _merge_source_data(combined, "SPOG_XPMS", source_data)

        assert combined["property"]["propertyId"] == "TEST-001"
        assert combined["dailyKPIs"]["occupancy"]["current"] == 90
        assert len(combined["actionItems"]) == 1

    def test_merge_loyalty_data(self) -> None:
        """Loyalty data merges into vipArrivals and actionItems."""
        combined: dict = {
            "property": {},
            "dailyKPIs": {},
            "actionItems": [],
            "vipArrivals": [],
        }
        source_data = {
            "vipArrivals": [{"guestName": "Test Guest"}],
            "actionItems": [{"id": "vip-1", "type": "VIP_ARRIVAL_ALERT"}],
        }

        _merge_source_data(combined, "SPOG_LOYALTY_CRM", source_data)

        assert len(combined["vipArrivals"]) == 1
        assert combined["vipArrivals"][0]["guestName"] == "Test Guest"
        assert len(combined["actionItems"]) == 1


# -- Tests for action_prioritizer --


class TestActionPrioritizer:
    """Tests for action item sorting by severity."""

    def test_sorts_by_severity_order(self) -> None:
        """Actions sorted URGENT > HIGH > MEDIUM > LOW."""
        raw_data = {
            "actionItems": [
                {"id": "1", "severity": "LOW", "title": "Low item"},
                {"id": "2", "severity": "URGENT", "title": "Urgent item"},
                {"id": "3", "severity": "MEDIUM", "title": "Medium item"},
                {"id": "4", "severity": "HIGH", "title": "High item"},
            ]
        }

        result = prioritize_actions(raw_data)

        assert result[0]["severity"] == "URGENT"
        assert result[1]["severity"] == "HIGH"
        assert result[2]["severity"] == "MEDIUM"
        assert result[3]["severity"] == "LOW"

    def test_empty_action_items_returns_empty(self) -> None:
        """Empty input returns empty list."""
        result = prioritize_actions({"actionItems": []})
        assert result == []

    def test_missing_action_items_key_returns_empty(self) -> None:
        """Missing actionItems key returns empty list."""
        result = prioritize_actions({})
        assert result == []

    def test_same_severity_maintains_order(self) -> None:
        """Items with same severity maintain original insertion order."""
        raw_data = {
            "actionItems": [
                {"id": "first", "severity": "URGENT", "title": "First urgent"},
                {"id": "second", "severity": "URGENT", "title": "Second urgent"},
            ]
        }

        result = prioritize_actions(raw_data)

        assert result[0]["id"] == "first"
        assert result[1]["id"] == "second"

    def test_unknown_severity_treated_as_lowest(self) -> None:
        """Unknown severity values sort after LOW."""
        raw_data = {
            "actionItems": [
                {"id": "unknown", "severity": "UNKNOWN", "title": "Unknown"},
                {"id": "low", "severity": "LOW", "title": "Low item"},
            ]
        }

        result = prioritize_actions(raw_data)

        assert result[0]["severity"] == "LOW"
        assert result[1]["severity"] == "UNKNOWN"
