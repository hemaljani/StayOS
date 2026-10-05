"""Unit tests for the dataset_generator.rooms_generator module.

Tests room inventory generation including room count distribution, floor
assignment, view assignment, premium status determination, room number
generation, and room status reconciliation.
"""

from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from dataset_generator.config import PROPERTY_PROFILES, PROPERTY_VIEWS
from dataset_generator.rooms_generator import (
    AMENITIES_BY_ROOM_TYPE,
    MAX_OCCUPANCY_BY_ROOM_TYPE,
    _assign_floor,
    _assign_view,
    _compute_room_counts,
    _generate_room_number,
    _is_premium_room,
    generate_rooms,
    reconcile_room_status,
)


# ---------------------------------------------------------------------------
# _compute_room_counts tests
# ---------------------------------------------------------------------------


class TestComputeRoomCounts:
    """Tests for room count distribution calculation."""

    def test_sum_equals_total_rooms(self) -> None:
        """Room count sum must equal the input total exactly."""
        for profile in PROPERTY_PROFILES:
            counts = _compute_room_counts(profile["totalRooms"])
            assert sum(counts.values()) == profile["totalRooms"]

    def test_all_room_types_present(self) -> None:
        """All 5 room types must have at least 1 room."""
        counts = _compute_room_counts(368)
        expected_types = {"PENTHOUSE", "SUITE", "KING_DELUXE", "QUEEN_DELUXE", "KING_STANDARD"}
        assert set(counts.keys()) == expected_types
        assert all(count > 0 for count in counts.values())

    def test_premium_about_5_percent(self) -> None:
        """PENTHOUSE + SUITE should be approximately 5% of total."""
        counts = _compute_room_counts(368)
        premium_pct = (counts["PENTHOUSE"] + counts["SUITE"]) / 368 * 100
        assert 3.0 <= premium_pct <= 7.0

    def test_deluxe_about_30_percent(self) -> None:
        """KING_DELUXE + QUEEN_DELUXE should be approximately 30% of total."""
        counts = _compute_room_counts(368)
        deluxe_pct = (counts["KING_DELUXE"] + counts["QUEEN_DELUXE"]) / 368 * 100
        assert 25.0 <= deluxe_pct <= 35.0

    def test_standard_about_65_percent(self) -> None:
        """KING_STANDARD should be approximately 65% of total."""
        counts = _compute_room_counts(368)
        standard_pct = counts["KING_STANDARD"] / 368 * 100
        assert 60.0 <= standard_pct <= 70.0

    def test_king_standard_absorbs_remainder(self) -> None:
        """KING_STANDARD gets whatever remains after rounding other types."""
        counts = _compute_room_counts(100)
        non_standard = (
            counts["PENTHOUSE"] + counts["SUITE"]
            + counts["KING_DELUXE"] + counts["QUEEN_DELUXE"]
        )
        assert counts["KING_STANDARD"] == 100 - non_standard


# ---------------------------------------------------------------------------
# _assign_floor tests
# ---------------------------------------------------------------------------


class TestAssignFloor:
    """Tests for floor assignment logic."""

    def test_penthouse_in_range(self) -> None:
        """Penthouse rooms are assigned to floors 20-24."""
        for i in range(7):
            floor = _assign_floor("PENTHOUSE", i, 7)
            assert 20 <= floor <= 24

    def test_suite_in_range(self) -> None:
        """Suite rooms are assigned to floors 18-22."""
        for i in range(11):
            floor = _assign_floor("SUITE", i, 11)
            assert 18 <= floor <= 22

    def test_king_standard_in_range(self) -> None:
        """Standard rooms are assigned to floors 2-11."""
        for i in range(240):
            floor = _assign_floor("KING_STANDARD", i, 240)
            assert 2 <= floor <= 11

    def test_deterministic(self) -> None:
        """Same inputs always produce same floor."""
        floor_a = _assign_floor("KING_DELUXE", 5, 55)
        floor_b = _assign_floor("KING_DELUXE", 5, 55)
        assert floor_a == floor_b


# ---------------------------------------------------------------------------
# _assign_view tests
# ---------------------------------------------------------------------------


class TestAssignView:
    """Tests for view assignment logic."""

    def test_returns_valid_view_for_property(self) -> None:
        """Assigned view must be in the property's view list."""
        for property_id, views in PROPERTY_VIEWS.items():
            for room_index in range(50):
                view = _assign_view(property_id, room_index, 10)
                assert view in views

    def test_deterministic(self) -> None:
        """Same inputs always produce same view."""
        view_a = _assign_view("ALOHA-CHI-001", 7, 12)
        view_b = _assign_view("ALOHA-CHI-001", 7, 12)
        assert view_a == view_b

    def test_cycles_through_views(self) -> None:
        """Different room indices produce different views (cycling)."""
        views_assigned = set()
        for room_index in range(20):
            view = _assign_view("ALOHA-CHI-001", room_index, 5)
            views_assigned.add(view)
        # Should have multiple different views
        assert len(views_assigned) > 1


# ---------------------------------------------------------------------------
# _is_premium_room tests
# ---------------------------------------------------------------------------


class TestIsPremiumRoom:
    """Tests for premium room classification."""

    def test_suite_always_premium(self) -> None:
        """SUITE is always premium regardless of floor."""
        assert _is_premium_room("SUITE", 2) is True
        assert _is_premium_room("SUITE", 18) is True

    def test_penthouse_always_premium(self) -> None:
        """PENTHOUSE is always premium regardless of floor."""
        assert _is_premium_room("PENTHOUSE", 20) is True

    def test_king_deluxe_premium_on_high_floor(self) -> None:
        """KING_DELUXE is premium on floor >= 15."""
        assert _is_premium_room("KING_DELUXE", 15) is True
        assert _is_premium_room("KING_DELUXE", 17) is True

    def test_king_deluxe_not_premium_on_low_floor(self) -> None:
        """KING_DELUXE is not premium below floor 15."""
        assert _is_premium_room("KING_DELUXE", 14) is False
        assert _is_premium_room("KING_DELUXE", 12) is False

    def test_queen_deluxe_never_premium(self) -> None:
        """QUEEN_DELUXE is never premium."""
        assert _is_premium_room("QUEEN_DELUXE", 14) is False
        assert _is_premium_room("QUEEN_DELUXE", 20) is False

    def test_king_standard_never_premium(self) -> None:
        """KING_STANDARD is never premium."""
        assert _is_premium_room("KING_STANDARD", 11) is False
        assert _is_premium_room("KING_STANDARD", 2) is False


# ---------------------------------------------------------------------------
# _generate_room_number tests
# ---------------------------------------------------------------------------


class TestGenerateRoomNumber:
    """Tests for room number string generation."""

    def test_basic_format(self) -> None:
        """Room number follows floor * 100 + sequence pattern."""
        assert _generate_room_number(12, 3) == "1203"
        assert _generate_room_number(2, 1) == "201"
        assert _generate_room_number(20, 15) == "2015"

    def test_returns_string(self) -> None:
        """Room number is always returned as a string."""
        result = _generate_room_number(5, 7)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# generate_rooms tests
# ---------------------------------------------------------------------------


class TestGenerateRooms:
    """Tests for the full room inventory generation function."""

    def setup_method(self) -> None:
        """Create a mock writer for each test."""
        self.mock_writer = MagicMock()
        self.mock_writer.write_items.return_value = {
            "success": 25,
            "failed": 0,
            "skipped": 0,
        }

    def test_returns_all_properties(self) -> None:
        """Lookup dict contains all 5 properties."""
        result = generate_rooms(self.mock_writer)
        expected_ids = {p["propertyId"] for p in PROPERTY_PROFILES}
        assert set(result.keys()) == expected_ids

    def test_correct_room_counts_per_property(self) -> None:
        """Each property has its expected totalRooms count."""
        result = generate_rooms(self.mock_writer)
        for profile in PROPERTY_PROFILES:
            assert len(result[profile["propertyId"]]) == profile["totalRooms"]

    def test_total_rooms_approximately_2000(self) -> None:
        """Total across all properties should be ~2,000."""
        result = generate_rooms(self.mock_writer)
        total = sum(len(rooms) for rooms in result.values())
        assert 1900 <= total <= 2100

    def test_all_rooms_start_available(self) -> None:
        """Every room item has status AVAILABLE initially."""
        result = generate_rooms(self.mock_writer)
        for rooms in result.values():
            for room in rooms:
                assert room["status"] == "AVAILABLE"

    def test_status_room_number_composite_key(self) -> None:
        """statusRoomNumber follows AVAILABLE#roomNumber format."""
        result = generate_rooms(self.mock_writer)
        for rooms in result.values():
            for room in rooms:
                expected = f"AVAILABLE#{room['roomNumber']}"
                assert room["statusRoomNumber"] == expected

    def test_room_item_has_all_required_fields(self) -> None:
        """Each room item contains all required DynamoDB attributes."""
        required = {
            "propertyId", "roomNumber", "roomType", "floor", "view",
            "status", "statusRoomNumber", "isPremiumRoom", "currentGuestId",
            "currentWorkOrderId", "maxOccupancy", "amenities",
        }
        result = generate_rooms(self.mock_writer)
        first_room = result["ALOHA-CHI-001"][0]
        assert required.issubset(set(first_room.keys()))

    def test_unique_room_numbers_per_property(self) -> None:
        """No duplicate room numbers within a single property."""
        result = generate_rooms(self.mock_writer)
        for prop_id, rooms in result.items():
            room_numbers = [r["roomNumber"] for r in rooms]
            assert len(room_numbers) == len(set(room_numbers)), (
                f"Duplicate room numbers in {prop_id}"
            )

    def test_writer_called_for_each_property(self) -> None:
        """Writer.write_items is called once per property."""
        generate_rooms(self.mock_writer)
        assert self.mock_writer.write_items.call_count == 5

    def test_amenities_match_room_type(self) -> None:
        """Room amenities correspond to the room's type."""
        result = generate_rooms(self.mock_writer)
        for rooms in result.values():
            for room in rooms:
                expected_amenities = AMENITIES_BY_ROOM_TYPE[room["roomType"]]
                assert room["amenities"] == expected_amenities

    def test_max_occupancy_matches_room_type(self) -> None:
        """Max occupancy corresponds to the room's type."""
        result = generate_rooms(self.mock_writer)
        for rooms in result.values():
            for room in rooms:
                expected = MAX_OCCUPANCY_BY_ROOM_TYPE[room["roomType"]]
                assert room["maxOccupancy"] == expected


# ---------------------------------------------------------------------------
# reconcile_room_status tests
# ---------------------------------------------------------------------------


class TestReconcileRoomStatus:
    """Tests for room status reconciliation logic."""

    def _make_rooms_lookup(self) -> Dict[str, List[Dict[str, Any]]]:
        """Create a minimal rooms lookup for testing."""
        return {
            "ALOHA-CHI-001": [
                {"propertyId": "ALOHA-CHI-001", "roomNumber": "1201", "isPremiumRoom": True},
                {"propertyId": "ALOHA-CHI-001", "roomNumber": "1202", "isPremiumRoom": False},
                {"propertyId": "ALOHA-CHI-001", "roomNumber": "501", "isPremiumRoom": False},
            ],
        }

    @staticmethod
    def _calls_for_status(mock_client: MagicMock, status: str) -> List[Any]:
        """Return the update_item calls whose ``:status`` value equals ``status``.

        Reconciliation now also emits explicit AVAILABLE resets for every room
        with no active reservation/work order (review finding CR-2), so a test
        that cares about one status must select that status's call(s) rather
        than assuming a single update_item call.
        """
        matched = []
        for call in mock_client.update_item.call_args_list:
            values = call[1]["ExpressionAttributeValues"]
            if values[":status"] == {"S": status}:
                matched.append(call)
        return matched

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_checked_in_sets_occupied(self, mock_client: MagicMock) -> None:
        """CHECKED_IN reservation marks room as OCCUPIED."""
        mock_client.update_item.return_value = {}

        reservations = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "CHECKED_IN",
                "guestId": "GUEST-001",
            },
        ]
        work_orders: List[Dict[str, Any]] = []

        result = reconcile_room_status(
            reservations, work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result["occupied"] == 1
        # Room 1201 is OCCUPIED; the other two rooms (1202, 501) are reset to
        # AVAILABLE, so reconcile now issues 3 update_item calls total.
        assert result["available"] == 2
        assert mock_client.update_item.call_count == 3
        occupied_calls = self._calls_for_status(mock_client, "OCCUPIED")
        assert len(occupied_calls) == 1
        call_kwargs = occupied_calls[0][1]
        assert call_kwargs["ExpressionAttributeValues"][":status"] == {"S": "OCCUPIED"}
        assert call_kwargs["ExpressionAttributeValues"][":gid"] == {"S": "GUEST-001"}

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_high_priority_work_order_sets_ooo(self, mock_client: MagicMock) -> None:
        """HIGH priority OPEN work order marks room as OOO."""
        mock_client.update_item.return_value = {}

        reservations: List[Dict[str, Any]] = []
        work_orders = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1202",
                "status": "OPEN",
                "priority": "HIGH",
                "workOrderId": "WO-001",
            },
        ]

        result = reconcile_room_status(
            reservations, work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result["ooo"] == 1
        ooo_calls = self._calls_for_status(mock_client, "OOO")
        assert len(ooo_calls) == 1
        call_kwargs = ooo_calls[0][1]
        assert call_kwargs["ExpressionAttributeValues"][":status"] == {"S": "OOO"}
        assert call_kwargs["ExpressionAttributeValues"][":woid"] == {"S": "WO-001"}

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_low_priority_work_order_sets_maintenance(self, mock_client: MagicMock) -> None:
        """LOW priority OPEN work order marks room as MAINTENANCE."""
        mock_client.update_item.return_value = {}

        reservations: List[Dict[str, Any]] = []
        work_orders = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "501",
                "status": "IN_PROGRESS",
                "priority": "LOW",
                "workOrderId": "WO-002",
            },
        ]

        result = reconcile_room_status(
            reservations, work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result["maintenance"] == 1
        maintenance_calls = self._calls_for_status(mock_client, "MAINTENANCE")
        assert len(maintenance_calls) == 1
        call_kwargs = maintenance_calls[0][1]
        assert call_kwargs["ExpressionAttributeValues"][":status"] == {"S": "MAINTENANCE"}

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_work_order_takes_precedence_over_reservation(
        self, mock_client: MagicMock
    ) -> None:
        """Work order OOO/MAINTENANCE takes precedence over OCCUPIED status."""
        mock_client.update_item.return_value = {}

        # Same room has both a checked-in reservation and a critical work order
        reservations = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "CHECKED_IN",
                "guestId": "GUEST-001",
            },
        ]
        work_orders = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "OPEN",
                "priority": "CRITICAL",
                "workOrderId": "WO-003",
            },
        ]

        result = reconcile_room_status(
            reservations, work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        # OOO from work order takes precedence
        assert result["ooo"] == 1
        assert result["occupied"] == 0
        # 1201 is OOO; 1202 and 501 are reset to AVAILABLE -> 3 updates total,
        # but only ONE OOO call for room 1201 (no OCCUPIED downgrade).
        assert self._calls_for_status(mock_client, "OOO") and \
            len(self._calls_for_status(mock_client, "OOO")) == 1
        assert self._calls_for_status(mock_client, "OCCUPIED") == []
        assert result["available"] == 2
        assert mock_client.update_item.call_count == 3

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_ignores_non_checked_in_reservations(self, mock_client: MagicMock) -> None:
        """Only CHECKED_IN reservations contribute to OCCUPIED status."""
        reservations = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "CONFIRMED",
                "guestId": "GUEST-001",
            },
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1202",
                "status": "CHECKED_OUT",
                "guestId": "GUEST-002",
            },
        ]
        work_orders: List[Dict[str, Any]] = []

        result = reconcile_room_status(
            reservations, work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result["occupied"] == 0
        # No room is occupied, so all 3 rooms are reset to AVAILABLE.
        assert result["available"] == 3
        assert self._calls_for_status(mock_client, "OCCUPIED") == []
        assert len(self._calls_for_status(mock_client, "AVAILABLE")) == 3

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_ignores_resolved_work_orders(self, mock_client: MagicMock) -> None:
        """Only OPEN and IN_PROGRESS work orders affect room status."""
        reservations: List[Dict[str, Any]] = []
        work_orders = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "RESOLVED",
                "priority": "HIGH",
                "workOrderId": "WO-004",
            },
        ]

        result = reconcile_room_status(
            reservations, work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result["ooo"] == 0
        assert result["maintenance"] == 0
        # A RESOLVED work order leaves its room without an active WO, so all 3
        # rooms are reset to AVAILABLE.
        assert result["available"] == 3
        assert len(self._calls_for_status(mock_client, "AVAILABLE")) == 3

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_handles_client_error_gracefully(self, mock_client: MagicMock) -> None:
        """ClientError during update increments error count."""
        mock_client.update_item.side_effect = ClientError(
            error_response={
                "Error": {"Code": "ConditionalCheckFailedException", "Message": "Condition not met"},
            },
            operation_name="UpdateItem",
        )

        reservations = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "CHECKED_IN",
                "guestId": "GUEST-001",
            },
        ]

        result = reconcile_room_status(
            reservations, [], self._make_rooms_lookup(), "stayos-rooms"
        )

        # Every update_item raises, so each attempted room (1 occupied + 2
        # available resets) is counted as an error and none succeed.
        assert result["errors"] == 3
        assert result["occupied"] == 0
        assert result["available"] == 0

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_empty_inputs_reset_all_rooms_available(self, mock_client: MagicMock) -> None:
        """No reservations or work orders resets every room to AVAILABLE.

        Previously this asserted "no updates needed", which was the CR-2 bug:
        rooms with no active reservation/work order were silently left in their
        prior status. Reconciliation now explicitly resets them, so with empty
        inputs all 3 rooms are moved to AVAILABLE.
        """
        mock_client.update_item.return_value = {}

        result = reconcile_room_status(
            [], [], self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result == {
            "occupied": 0,
            "ooo": 0,
            "maintenance": 0,
            "available": 3,
            "errors": 0,
        }
        assert mock_client.update_item.call_count == 3
        for call in mock_client.update_item.call_args_list:
            values = call[1]["ExpressionAttributeValues"]
            assert values[":status"] == {"S": "AVAILABLE"}
            assert values[":gid"] == {"NULL": True}
            assert values[":woid"] == {"NULL": True}

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_status_room_number_composite_key_in_update(
        self, mock_client: MagicMock
    ) -> None:
        """UpdateItem sets statusRoomNumber to status#roomNumber."""
        mock_client.update_item.return_value = {}

        reservations = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "CHECKED_IN",
                "guestId": "GUEST-001",
            },
        ]

        reconcile_room_status(
            reservations, [], self._make_rooms_lookup(), "stayos-rooms"
        )

        occupied_calls = self._calls_for_status(mock_client, "OCCUPIED")
        assert len(occupied_calls) == 1
        call_kwargs = occupied_calls[0][1]
        assert call_kwargs["ExpressionAttributeValues"][":srn"] == {"S": "OCCUPIED#1201"}


    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_work_order_missing_room_number_is_skipped(
        self, mock_client: MagicMock
    ) -> None:
        """A work order with no roomNumber is ignored (defensive continue)."""
        mock_client.update_item.return_value = {}
        work_orders = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "",  # missing -> skip this work order
                "status": "OPEN",
                "priority": "HIGH",
                "workOrderId": "WO-BAD",
            },
        ]

        result = reconcile_room_status(
            [], work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        # No OOO applied; all 3 rooms reset to AVAILABLE instead.
        assert result["ooo"] == 0
        assert result["available"] == 3
        assert self._calls_for_status(mock_client, "OOO") == []

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_reservation_missing_room_number_is_skipped(
        self, mock_client: MagicMock
    ) -> None:
        """A CHECKED_IN reservation with no roomNumber is ignored."""
        mock_client.update_item.return_value = {}
        reservations = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "",  # missing -> skip this reservation
                "status": "CHECKED_IN",
                "guestId": "GUEST-BAD",
            },
        ]

        result = reconcile_room_status(
            reservations, [], self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result["occupied"] == 0
        assert result["available"] == 3

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_higher_priority_work_order_not_downgraded(
        self, mock_client: MagicMock
    ) -> None:
        """A second lower-priority work order on an OOO room does not downgrade it."""
        mock_client.update_item.return_value = {}
        work_orders = [
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "OPEN",
                "priority": "CRITICAL",  # -> OOO
                "workOrderId": "WO-1",
            },
            {
                "propertyId": "ALOHA-CHI-001",
                "roomNumber": "1201",
                "status": "OPEN",
                "priority": "LOW",  # would be MAINTENANCE, but OOO must win
                "workOrderId": "WO-2",
            },
        ]

        result = reconcile_room_status(
            [], work_orders, self._make_rooms_lookup(), "stayos-rooms"
        )

        assert result["ooo"] == 1
        assert result["maintenance"] == 0
        ooo_calls = self._calls_for_status(mock_client, "OOO")
        assert len(ooo_calls) == 1
        assert ooo_calls[0][1]["ExpressionAttributeValues"][":woid"] == {"S": "WO-1"}

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_room_without_room_number_is_not_reset(
        self, mock_client: MagicMock
    ) -> None:
        """A room lookup entry with no roomNumber is skipped by the reset loop."""
        mock_client.update_item.return_value = {}
        rooms_lookup = {
            "ALOHA-CHI-001": [
                {"propertyId": "ALOHA-CHI-001", "roomNumber": "1201"},
                {"propertyId": "ALOHA-CHI-001", "roomNumber": ""},  # skipped
            ],
        }

        result = reconcile_room_status([], [], rooms_lookup, "stayos-rooms")

        # Only the one valid room is reset to AVAILABLE.
        assert result["available"] == 1
        assert mock_client.update_item.call_count == 1

    @patch("dataset_generator.rooms_generator._dynamodb_client")
    def test_stale_status_reset_to_available_on_reconcile(
        self, mock_client: MagicMock
    ) -> None:
        """A room OCCUPIED on one reference date is reset when the res is gone.

        Feature: data-orchestrator, review finding CR-2 (idempotency under the
        real Generate -> Reconcile -> Reconcile flow). The first reconcile marks
        room 1201 OCCUPIED. The second reconcile runs with NO active reservation
        or work order for 1201, so it MUST emit an explicit reset to AVAILABLE
        rather than leaving the stale OCCUPIED status behind. This is the exact
        gap the old code missed: it only ever wrote rooms with a current
        reservation/work order and never reset the others.
        """
        mock_client.update_item.return_value = {}
        rooms_lookup = self._make_rooms_lookup()

        # Day N: 1201 is CHECKED_IN -> OCCUPIED.
        first = reconcile_room_status(
            [
                {
                    "propertyId": "ALOHA-CHI-001",
                    "roomNumber": "1201",
                    "status": "CHECKED_IN",
                    "guestId": "GUEST-001",
                }
            ],
            [],
            rooms_lookup,
            "stayos-rooms",
        )
        assert first["occupied"] == 1
        assert first["available"] == 2  # 1202, 501

        # Day N+1: the reservation is gone; no active res/WO for any room.
        mock_client.reset_mock()
        second = reconcile_room_status([], [], rooms_lookup, "stayos-rooms")

        # 1201 is no longer occupied and MUST be reset to AVAILABLE, along with
        # the other two rooms -> all 3 rooms reset, none left OCCUPIED.
        assert second["occupied"] == 0
        assert second["available"] == 3
        reset_rooms = {
            call[1]["Key"]["roomNumber"]["S"]
            for call in self._calls_for_status(mock_client, "AVAILABLE")
        }
        assert reset_rooms == {"1201", "1202", "501"}
        # The 1201 reset clears the previous guest/composite key.
        room_1201_reset = next(
            call
            for call in self._calls_for_status(mock_client, "AVAILABLE")
            if call[1]["Key"]["roomNumber"]["S"] == "1201"
        )
        values = room_1201_reset[1]["ExpressionAttributeValues"]
        assert values[":srn"] == {"S": "AVAILABLE#1201"}
        assert values[":gid"] == {"NULL": True}
        assert values[":woid"] == {"NULL": True}
