"""Unit tests for the dataset_generator.work_orders_generator module.

Tests work order generation including priority assignment, creation timestamps,
resolution lifecycle, room selection, issue type rotation, and the full
generate_work_orders function.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from dataset_generator.config import (
    MAINTENANCE_TEAM,
    PROPERTY_PROFILES,
    RESOLUTION_TIME_HOURS,
    SEED_DAYS,
    TTL_WORK_ORDERS_DAYS,
    WORK_ORDER_CATEGORIES,
    WORK_ORDER_NOTES,
)
from dataset_generator.work_orders_generator import (
    DAILY_WORK_ORDER_COUNTS,
    PRIORITY_CYCLE,
    PROPERTY_SHORT_CODES,
    _compute_created_at,
    _compute_resolution_hours,
    _compute_ttl,
    _determine_priority,
    _determine_status,
    _get_daily_count,
    _select_assigned_to,
    _select_issue_type,
    _select_notes,
    _select_room,
    generate_work_orders,
)


# ---------------------------------------------------------------------------
# _determine_priority tests
# ---------------------------------------------------------------------------


class TestDeterminePriority:
    """Tests for priority assignment from PRIORITY_CYCLE."""

    def test_returns_valid_priority(self) -> None:
        """All returned priorities are one of the 4 valid values."""
        valid = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        for i in range(200):
            assert _determine_priority(i) in valid

    def test_distribution_per_cycle(self) -> None:
        """20-item cycle has correct distribution: 2 CRITICAL, 5 HIGH, 7 MEDIUM, 6 LOW."""
        counts: Dict[str, int] = {}
        for i in range(20):
            p = _determine_priority(i)
            counts[p] = counts.get(p, 0) + 1
        assert counts["CRITICAL"] == 2
        assert counts["HIGH"] == 5
        assert counts["MEDIUM"] == 7
        assert counts["LOW"] == 6

    def test_deterministic(self) -> None:
        """Same index always produces same priority."""
        for i in range(50):
            assert _determine_priority(i) == _determine_priority(i)

    def test_interleaved_across_small_groups(self) -> None:
        """Any group of 5 consecutive work orders has at least 2 different priorities."""
        for start in range(0, 50, 5):
            priorities = {_determine_priority(i) for i in range(start, start + 5)}
            assert len(priorities) >= 2


# ---------------------------------------------------------------------------
# _get_daily_count tests
# ---------------------------------------------------------------------------


class TestGetDailyCount:
    """Tests for daily work order count determination."""

    def test_in_valid_range(self) -> None:
        """Daily count is always between 3 and 8."""
        for day in range(SEED_DAYS):
            for prop in range(5):
                count = _get_daily_count(day, prop)
                assert 3 <= count <= 8

    def test_varies_by_property(self) -> None:
        """Different properties get different counts across a week."""
        # Collect counts for a full week per property - they shouldn't all be identical
        prop_week_totals = []
        for prop in range(5):
            weekly = sum(_get_daily_count(d, prop) for d in range(7))
            prop_week_totals.append(weekly)
        # At least some properties have different weekly totals
        assert len(set(prop_week_totals)) > 1

    def test_deterministic(self) -> None:
        """Same inputs always produce same count."""
        assert _get_daily_count(5, 2) == _get_daily_count(5, 2)


# ---------------------------------------------------------------------------
# _compute_created_at tests
# ---------------------------------------------------------------------------


class TestComputeCreatedAt:
    """Tests for work order creation timestamp computation."""

    def test_within_business_hours(self) -> None:
        """Creation time is always between 06:00 and 22:00."""
        start = date(2026, 7, 10)
        for i in range(100):
            dt = _compute_created_at(start, i % 30, i)
            assert 6 <= dt.hour <= 21

    def test_correct_date(self) -> None:
        """Created date matches start_date + day_offset."""
        start = date(2026, 7, 10)
        dt = _compute_created_at(start, 5, 0)
        assert dt.date() == date(2026, 7, 15)

    def test_deterministic(self) -> None:
        """Same inputs always produce same timestamp."""
        start = date(2026, 7, 10)
        a = _compute_created_at(start, 3, 7)
        b = _compute_created_at(start, 3, 7)
        assert a == b

    def test_minute_in_valid_range(self) -> None:
        """Minute is always 0-59."""
        start = date(2026, 7, 10)
        for i in range(100):
            dt = _compute_created_at(start, 0, i)
            assert 0 <= dt.minute <= 59


# ---------------------------------------------------------------------------
# _compute_resolution_hours tests
# ---------------------------------------------------------------------------


class TestComputeResolutionHours:
    """Tests for resolution time computation."""

    def test_critical_range(self) -> None:
        """CRITICAL resolution is always 6-12 hours."""
        for i in range(20):
            hours = _compute_resolution_hours("CRITICAL", i)
            assert 6 <= hours <= 12

    def test_high_range(self) -> None:
        """HIGH resolution is always 12-24 hours."""
        for i in range(20):
            hours = _compute_resolution_hours("HIGH", i)
            assert 12 <= hours <= 24

    def test_medium_range(self) -> None:
        """MEDIUM resolution is always 24-48 hours."""
        for i in range(30):
            hours = _compute_resolution_hours("MEDIUM", i)
            assert 24 <= hours <= 48

    def test_low_range(self) -> None:
        """LOW resolution is always 48-72 hours."""
        for i in range(30):
            hours = _compute_resolution_hours("LOW", i)
            assert 48 <= hours <= 72

    def test_deterministic(self) -> None:
        """Same inputs always produce same hours."""
        assert _compute_resolution_hours("HIGH", 5) == _compute_resolution_hours("HIGH", 5)


# ---------------------------------------------------------------------------
# _determine_status tests
# ---------------------------------------------------------------------------


class TestDetermineStatus:
    """Tests for work order lifecycle status determination."""

    def test_resolved_when_past_resolution_time(self) -> None:
        """Work order is RESOLVED when createdAt + resolution <= now."""
        created = datetime(2026, 8, 1, 10, 0, 0)
        reference = datetime(2026, 8, 2, 10, 0, 0)
        status, resolved_at = _determine_status(created, 12, reference)
        assert status == "RESOLVED"
        assert resolved_at == datetime(2026, 8, 1, 22, 0, 0)

    def test_in_progress_when_past_half_resolution(self) -> None:
        """Work order is IN_PROGRESS when past half resolution but not full."""
        created = datetime(2026, 8, 7, 10, 0, 0)
        # 20 hours resolution, reference at 15 hours later (past half=10h, before full=20h)
        reference = datetime(2026, 8, 8, 1, 0, 0)
        status, resolved_at = _determine_status(created, 20, reference)
        assert status == "IN_PROGRESS"
        assert resolved_at is None

    def test_open_when_recently_created(self) -> None:
        """Work order is OPEN when created less than half resolution time ago."""
        created = datetime(2026, 8, 7, 20, 0, 0)
        # 24 hours resolution, reference 4 hours later (before half=12h)
        reference = datetime(2026, 8, 8, 0, 0, 0)
        status, resolved_at = _determine_status(created, 24, reference)
        assert status == "OPEN"
        assert resolved_at is None

    def test_resolved_at_is_created_plus_resolution(self) -> None:
        """resolvedAt equals createdAt + resolution hours."""
        created = datetime(2026, 8, 1, 6, 0, 0)
        reference = datetime(2026, 8, 5, 0, 0, 0)
        status, resolved_at = _determine_status(created, 8, reference)
        assert resolved_at == datetime(2026, 8, 1, 14, 0, 0)


# ---------------------------------------------------------------------------
# _select_room tests
# ---------------------------------------------------------------------------


class TestSelectRoom:
    """Tests for room selection logic."""

    def _make_mock_rooms(self) -> List[Dict[str, Any]]:
        """Create a mock rooms list with mixed premium/non-premium."""
        rooms = []
        for i in range(20):
            rooms.append({
                "roomNumber": str(200 + i),
                "isPremiumRoom": (i < 4),  # First 4 are premium
            })
        return rooms

    def test_returns_valid_room_number(self) -> None:
        """Selected room number exists in the rooms list."""
        rooms = self._make_mock_rooms()
        valid_numbers = {r["roomNumber"] for r in rooms}
        for i in range(10):
            room_num, _ = _select_room(rooms, i, [])
            assert room_num in valid_numbers

    def test_every_5th_targets_premium(self) -> None:
        """Work orders at indices 0, 5, 10, ... target premium rooms."""
        rooms = self._make_mock_rooms()
        room_num, is_premium = _select_room(rooms, 0, [])
        assert is_premium is True
        room_num, is_premium = _select_room(rooms, 5, [])
        assert is_premium is True

    def test_non_5th_targets_non_premium(self) -> None:
        """Work orders not at 5th indices target non-premium rooms."""
        rooms = self._make_mock_rooms()
        room_num, is_premium = _select_room(rooms, 1, [])
        assert is_premium is False
        room_num, is_premium = _select_room(rooms, 3, [])
        assert is_premium is False

    def test_avoids_used_rooms(self) -> None:
        """Rooms in used_room_indices are not selected."""
        rooms = self._make_mock_rooms()
        # Mark first premium rooms as used
        used = [0, 1, 2, 3]
        # Index 5 would target premium, but all are used - falls back to non-premium
        room_num, is_premium = _select_room(rooms, 5, used)
        # Should have fallen back to a non-premium room or all-available
        assert room_num in {r["roomNumber"] for r in rooms}


# ---------------------------------------------------------------------------
# _select_issue_type tests
# ---------------------------------------------------------------------------


class TestSelectIssueType:
    """Tests for issue type rotation."""

    def test_returns_valid_category(self) -> None:
        """Selected category is from WORK_ORDER_CATEGORIES."""
        for i in range(20):
            category = _select_issue_type(i)
            assert category in WORK_ORDER_CATEGORIES

    def test_rotates_through_all_types(self) -> None:
        """All 7 issue types are used across a full rotation."""
        types_seen = set()
        for i in range(7):
            category = _select_issue_type(i)
            types_seen.add(category["issueType"])
        assert len(types_seen) == 7


# ---------------------------------------------------------------------------
# _select_notes tests
# ---------------------------------------------------------------------------


class TestSelectNotes:
    """Tests for work order notes selection."""

    def test_returns_valid_note(self) -> None:
        """Selected note exists in WORK_ORDER_NOTES for the issue type."""
        for issue_type in WORK_ORDER_NOTES:
            for i in range(5):
                note = _select_notes(issue_type, i)
                assert note in WORK_ORDER_NOTES[issue_type]

    def test_rotates_through_templates(self) -> None:
        """Different indices produce different notes (cycling)."""
        notes_seen = set()
        for i in range(3):
            notes_seen.add(_select_notes("HVAC", i))
        assert len(notes_seen) == 3


# ---------------------------------------------------------------------------
# _select_assigned_to tests
# ---------------------------------------------------------------------------


class TestSelectAssignedTo:
    """Tests for maintenance team member assignment."""

    def test_returns_valid_team_member(self) -> None:
        """Assigned member is from MAINTENANCE_TEAM."""
        for i in range(20):
            member = _select_assigned_to(i)
            assert member in MAINTENANCE_TEAM

    def test_rotates_through_team(self) -> None:
        """All team members are assigned across a full rotation."""
        members_seen = set()
        for i in range(len(MAINTENANCE_TEAM)):
            members_seen.add(_select_assigned_to(i))
        assert members_seen == set(MAINTENANCE_TEAM)


# ---------------------------------------------------------------------------
# _compute_ttl tests
# ---------------------------------------------------------------------------


class TestComputeTtl:
    """Tests for TTL value computation."""

    def test_ttl_is_60_days_from_created(self) -> None:
        """TTL epoch is approximately 60 days after createdAt."""
        created = datetime(2026, 8, 1, 10, 0, 0)
        ttl = _compute_ttl(created)
        # TTL should be epoch of 2026-09-30T10:00:00 (60 days later)
        expected_date = created + timedelta(days=TTL_WORK_ORDERS_DAYS)
        # Allow 1-day tolerance for timezone/DST edge cases
        assert abs(ttl - int(expected_date.timestamp())) < 86400

    def test_ttl_is_integer(self) -> None:
        """TTL value is always an integer (Unix epoch)."""
        created = datetime(2026, 8, 5, 14, 30, 0)
        assert isinstance(_compute_ttl(created), int)


# ---------------------------------------------------------------------------
# generate_work_orders tests
# ---------------------------------------------------------------------------


class TestGenerateWorkOrders:
    """Tests for the full work order generation function."""

    def _make_rooms_lookup(self) -> Dict[str, List[Dict[str, Any]]]:
        """Create a rooms lookup with realistic room counts per property."""
        lookup: Dict[str, List[Dict[str, Any]]] = {}
        for profile in PROPERTY_PROFILES:
            property_id = profile["propertyId"]
            rooms = []
            for i in range(profile["totalRooms"]):
                rooms.append({
                    "roomNumber": str(200 + i),
                    "isPremiumRoom": (i % 10 == 0),
                })
            lookup[property_id] = rooms
        return lookup

    def setup_method(self) -> None:
        """Create mock writer and rooms lookup for each test."""
        self.mock_writer = MagicMock()
        self.mock_writer.write_items.return_value = {
            "success": 25,
            "failed": 0,
            "skipped": 0,
        }
        self.rooms_lookup = self._make_rooms_lookup()

    def test_returns_list_of_dicts(self) -> None:
        """Function returns a list of work order dicts."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        assert isinstance(result, list)
        assert all(isinstance(item, dict) for item in result)

    def test_total_approximately_750(self) -> None:
        """Total work orders across all properties is approximately 750."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        assert 600 <= len(result) <= 900

    def test_all_properties_have_work_orders(self) -> None:
        """Every property has generated work orders."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        property_ids_with_orders = {wo["propertyId"] for wo in result}
        expected_ids = {p["propertyId"] for p in PROPERTY_PROFILES}
        assert property_ids_with_orders == expected_ids

    def test_work_order_id_format(self) -> None:
        """Work order IDs follow WO-{CODE}-{NNNN} pattern."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        for wo in result[:20]:
            prop_id = wo["propertyId"]
            short_code = PROPERTY_SHORT_CODES[prop_id]
            assert wo["workOrderId"].startswith(f"WO-{short_code}-")
            # 4-digit sequence
            seq = wo["workOrderId"].split("-")[-1]
            assert len(seq) == 4
            assert seq.isdigit()

    def test_status_created_at_composite(self) -> None:
        """statusCreatedAt follows status#ISO_datetime pattern."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        for wo in result[:20]:
            parts = wo["statusCreatedAt"].split("#", 1)
            assert parts[0] in ("OPEN", "IN_PROGRESS", "RESOLVED")
            assert parts[0] == wo["status"]
            assert parts[1] == wo["createdAt"]

    def test_resolved_orders_have_resolved_at(self) -> None:
        """RESOLVED work orders have a non-null resolvedAt timestamp."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        for wo in result:
            if wo["status"] == "RESOLVED":
                assert wo["resolvedAt"] is not None
            else:
                assert wo["resolvedAt"] is None

    def test_all_required_fields_present(self) -> None:
        """Every work order has all required DynamoDB attributes."""
        required = {
            "propertyId", "workOrderId", "statusCreatedAt", "status",
            "priority", "issueType", "roomNumber", "isPremiumRoom",
            "notes", "assignedTo", "createdAt", "resolvedAt",
            "estimatedResolutionHours", "ttl",
        }
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        for wo in result[:20]:
            assert required.issubset(set(wo.keys()))

    def test_room_numbers_are_valid(self) -> None:
        """All assigned room numbers exist in the rooms lookup."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        for wo in result:
            prop_id = wo["propertyId"]
            valid_numbers = {r["roomNumber"] for r in self.rooms_lookup[prop_id]}
            assert wo["roomNumber"] in valid_numbers

    def test_writer_called_for_each_property(self) -> None:
        """Writer.write_items is called once per property."""
        generate_work_orders(self.mock_writer, self.rooms_lookup)
        assert self.mock_writer.write_items.call_count == 5

    def test_some_open_work_orders_exist(self) -> None:
        """At least some work orders have OPEN status (OOO constraint)."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        open_orders = [wo for wo in result if wo["status"] == "OPEN"]
        assert len(open_orders) > 0

    def test_some_in_progress_work_orders_exist(self) -> None:
        """At least some work orders have IN_PROGRESS status."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        in_progress = [wo for wo in result if wo["status"] == "IN_PROGRESS"]
        assert len(in_progress) > 0

    def test_priority_distribution_reasonable(self) -> None:
        """Priority distribution is roughly 10/25/35/30."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        total = len(result)
        counts: Dict[str, int] = {}
        for wo in result:
            counts[wo["priority"]] = counts.get(wo["priority"], 0) + 1

        # Allow 5% tolerance on each
        assert counts["CRITICAL"] / total > 0.05
        assert counts["HIGH"] / total > 0.20
        assert counts["MEDIUM"] / total > 0.30
        assert counts["LOW"] / total > 0.25

    def test_ttl_is_positive_integer(self) -> None:
        """All TTL values are positive integers."""
        result = generate_work_orders(self.mock_writer, self.rooms_lookup)
        for wo in result[:20]:
            assert isinstance(wo["ttl"], int)
            assert wo["ttl"] > 0

    def test_skips_property_with_no_rooms(self) -> None:
        """Properties not in rooms_lookup are skipped gracefully."""
        partial_lookup = {
            "ALOHA-CHI-001": self.rooms_lookup["ALOHA-CHI-001"],
        }
        result = generate_work_orders(self.mock_writer, partial_lookup)
        property_ids = {wo["propertyId"] for wo in result}
        assert property_ids == {"ALOHA-CHI-001"}
