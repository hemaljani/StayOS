"""Unit tests for the AgentCore Gateway Tool Lambda PULSE tools.

Validates the three additive read-only PULSE tools registered on LUMI's
shared Gateway target (Decision 7):
    * get_sister_property_availability - sister-property availability shaping
      and empty-availability exclusion.
    * get_walkable_guests - the shortfall cap and the at-or-below loyalty
      threshold filter, ordered lowest-loyalty first.
    * get_room_move_candidates - roomType filtering and result shaping.

Property scoping (missing propertyId returns the "unavailable" envelope) is
verified through the router for every new tool. DynamoDB is mocked by patching
the module-level resource, mirroring the voice agent tool_handlers tests.

Validates: PULSE Requirements 3.3, 3.4, 3.5, 3.6, 4.2, 7.2
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

# Path to the Tool Lambda under test. It lives outside the pytest.ini
# pythonpath and shares the module name "lambda_function" with other
# functions, so it is loaded by file path under a unique module name.
_TOOLS_LAMBDA_PATH = (
    Path(__file__).resolve().parents[2] / "functions" / "tools" / "lambda_function.py"
)


def _load_tools_module() -> ModuleType:
    """Load the Tool Lambda module fresh under a unique name.

    Returns:
        The imported tools lambda_function module object.
    """
    spec = importlib.util.spec_from_file_location(
        "tools_lambda_function", _TOOLS_LAMBDA_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tools(monkeypatch: pytest.MonkeyPatch) -> Tuple[ModuleType, MagicMock]:
    """Load the tools module and patch its DynamoDB resource with a mock.

    Returns:
        Tuple of (module, mock DynamoDB resource). Configure table responses
        via ``mock_resource.Table.return_value.query.return_value = {...}``.
    """
    module = _load_tools_module()
    mock_resource = MagicMock()
    monkeypatch.setattr(module, "_dynamodb_resource", mock_resource)
    return module, mock_resource


# ---------------------------------------------------------------------------
# get_sister_property_availability
# ---------------------------------------------------------------------------


class TestSisterPropertyAvailability:
    """Tests for the sister-property availability tool (Requirement 3.4)."""

    def test_returns_same_brand_sisters_with_availability(
        self, tools: Tuple[ModuleType, MagicMock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sister properties of the same brand with available rooms are returned.

        The calling property is excluded and each sister reports its available
        room count.
        """
        module, mock_resource = tools
        monkeypatch.setattr(
            module,
            "_discover_estate_property_ids",
            lambda: ["ALOHA-CHI-001", "ALOHA-MIA-001", "ALOHA-TYO-001"],
        )
        # Every sister query returns two AVAILABLE rooms (single page).
        mock_resource.Table.return_value.query.return_value = {
            "Items": [
                {"roomNumber": "1201", "roomType": "SUITE", "status": "AVAILABLE"},
                {"roomNumber": "1202", "roomType": "SUITE", "status": "AVAILABLE"},
            ]
        }

        result = module.get_sister_property_availability(
            "ALOHA-CHI-001",
            {"startDate": "2025-06-01", "endDate": "2025-06-03"},
        )

        assert result["status"] == "success"
        data = result["data"]
        assert data["startDate"] == "2025-06-01"
        assert data["endDate"] == "2025-06-03"
        sister_ids = {s["propertyId"] for s in data["sisterProperties"]}
        # Calling property excluded; the two same-brand sisters are present.
        assert sister_ids == {"ALOHA-MIA-001", "ALOHA-TYO-001"}
        assert all(s["availableRooms"] == 2 for s in data["sisterProperties"])

    def test_excludes_sisters_without_availability(
        self, tools: Tuple[ModuleType, MagicMock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sister with zero available rooms is omitted from the result."""
        module, mock_resource = tools
        monkeypatch.setattr(
            module,
            "_discover_estate_property_ids",
            lambda: ["ALOHA-CHI-001", "ALOHA-MIA-001", "ALOHA-TYO-001"],
        )
        # First sister (MIA) has one room, second sister (TYO) has none.
        mock_resource.Table.return_value.query.side_effect = [
            {"Items": [{"roomNumber": "808", "roomType": "KING_STANDARD", "status": "AVAILABLE"}]},
            {"Items": []},
        ]

        result = module.get_sister_property_availability("ALOHA-CHI-001", {})

        sisters = result["data"]["sisterProperties"]
        assert len(sisters) == 1
        assert sisters[0]["propertyId"] == "ALOHA-MIA-001"
        assert sisters[0]["availableRooms"] == 1


# ---------------------------------------------------------------------------
# get_walkable_guests
# ---------------------------------------------------------------------------


def _reservation(guest_id: str, tier: str, reservation_id: str) -> Dict[str, Any]:
    """Build a minimal confirmed-arrival reservation item for tests.

    Args:
        guest_id: The guest identifier.
        tier: The loyalty tier string.
        reservation_id: The reservation identifier.

    Returns:
        A reservation dict shaped like a CONFIRMED arrival record.
    """
    return {
        "guestId": guest_id,
        "loyaltyTier": tier,
        "reservationId": reservation_id,
        "status": "CONFIRMED",
    }


class TestWalkableGuests:
    """Tests for walkable-guest selection (Requirements 3.3, 3.5, 3.6)."""

    def test_enforces_cap_and_threshold_lowest_first(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """Selection excludes above-threshold tiers, sorts lowest-first, and caps.

        With protection tier TITANIUM, AMBASSADOR guests are excluded; the
        remaining guests are ordered PLATINUM before TITANIUM and truncated to
        the shortfall.
        """
        module, mock_resource = tools
        mock_resource.Table.return_value.query.return_value = {
            "Items": [
                _reservation("G-1", "AMBASSADOR", "R-1"),
                _reservation("G-2", "TITANIUM", "R-2"),
                _reservation("G-3", "PLATINUM", "R-3"),
                _reservation("G-4", "PLATINUM", "R-4"),
            ]
        }

        result = module.get_walkable_guests(
            "ALOHA-CHI-001",
            {"shortfall": 2, "loyaltyProtectionTier": "TITANIUM", "arrivalDate": "2025-06-01"},
        )

        data = result["data"]
        assert result["status"] == "success"
        assert data["cappedAtShortfall"] == 2
        walkable = data["walkableGuests"]
        assert len(walkable) == 2
        # AMBASSADOR is above the TITANIUM threshold and must be excluded.
        assert all(g["loyaltyTier"] != "AMBASSADOR" for g in walkable)
        # Lowest-loyalty first: both PLATINUM guests are chosen before TITANIUM.
        assert [g["loyaltyTier"] for g in walkable] == ["PLATINUM", "PLATINUM"]
        assert {g["guestId"] for g in walkable} == {"G-3", "G-4"}
        assert walkable[0]["reservationId"] in {"R-3", "R-4"}

    def test_default_protection_tier_selects_only_least_elite(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """Without a protection tier, only PLATINUM (least elite) is walkable."""
        module, mock_resource = tools
        mock_resource.Table.return_value.query.return_value = {
            "Items": [
                _reservation("G-1", "AMBASSADOR", "R-1"),
                _reservation("G-2", "TITANIUM", "R-2"),
                _reservation("G-3", "PLATINUM", "R-3"),
            ]
        }

        result = module.get_walkable_guests("ALOHA-CHI-001", {"shortfall": 10})

        data = result["data"]
        assert data["loyaltyProtectionTier"] == "PLATINUM"
        assert data["cappedAtShortfall"] == 10
        assert [g["guestId"] for g in data["walkableGuests"]] == ["G-3"]

    def test_zero_shortfall_returns_no_guests(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """A non-positive shortfall yields an empty walkable list."""
        module, mock_resource = tools
        mock_resource.Table.return_value.query.return_value = {
            "Items": [_reservation("G-3", "PLATINUM", "R-3")]
        }

        result = module.get_walkable_guests("ALOHA-CHI-001", {"shortfall": 0})

        assert result["data"]["walkableGuests"] == []
        assert result["data"]["cappedAtShortfall"] == 0


# ---------------------------------------------------------------------------
# get_room_move_candidates
# ---------------------------------------------------------------------------


class TestRoomMoveCandidates:
    """Tests for room-move candidate selection (Requirement 4.2)."""

    def test_shapes_candidate_rooms(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """Available rooms are shaped into candidate room records."""
        module, mock_resource = tools
        mock_resource.Table.return_value.query.return_value = {
            "Items": [
                {"roomNumber": "1801", "roomType": "SUITE", "status": "AVAILABLE", "floor": 18},
                {"roomNumber": "1802", "roomType": "SUITE", "status": "AVAILABLE", "floor": 18},
            ]
        }

        result = module.get_room_move_candidates("ALOHA-CHI-001", {"roomType": "SUITE"})

        data = result["data"]
        assert result["status"] == "success"
        assert data["roomType"] == "SUITE"
        assert data["candidateRooms"] == [
            {"roomNumber": "1801", "roomType": "SUITE", "status": "AVAILABLE"},
            {"roomNumber": "1802", "roomType": "SUITE", "status": "AVAILABLE"},
        ]

    def test_roomtype_applies_filter_expression(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """Supplying roomType adds a DynamoDB FilterExpression to the query."""
        module, mock_resource = tools
        mock_resource.Table.return_value.query.return_value = {"Items": []}

        module.get_room_move_candidates("ALOHA-CHI-001", {"roomType": "PENTHOUSE"})

        _, query_kwargs = mock_resource.Table.return_value.query.call_args
        assert "FilterExpression" in query_kwargs

    def test_no_roomtype_omits_filter_expression(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """Omitting roomType issues an unfiltered availability query."""
        module, mock_resource = tools
        mock_resource.Table.return_value.query.return_value = {"Items": []}

        module.get_room_move_candidates("ALOHA-CHI-001", {})

        _, query_kwargs = mock_resource.Table.return_value.query.call_args
        assert "FilterExpression" not in query_kwargs


# ---------------------------------------------------------------------------
# Property scoping and registry
# ---------------------------------------------------------------------------


class TestPropertyScopingAndRegistry:
    """Tests for property scoping and the tool registry."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "get_sister_property_availability",
            "get_walkable_guests",
            "get_room_move_candidates",
        ],
    )
    def test_missing_property_id_returns_unavailable(
        self, tools: Tuple[ModuleType, MagicMock], tool_name: str
    ) -> None:
        """Routing a PULSE tool without propertyId returns the unavailable envelope."""
        module, _ = tools

        result = module._route_tool(tool_name, {"shortfall": 1})

        assert result["status"] == "unavailable"
        assert "propertyId" in result["message"]

    def test_registry_contains_new_and_existing_tools(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """The registry retains the original 5 tools and adds the 3 PULSE tools."""
        module, _ = tools
        registry_names = set(module.TOOL_REGISTRY)

        assert {
            "get_occupancy",
            "get_revenue",
            "get_vip_guests",
            "get_room_status",
            "get_work_orders",
        }.issubset(registry_names)
        assert {
            "get_sister_property_availability",
            "get_walkable_guests",
            "get_room_move_candidates",
        }.issubset(registry_names)



class TestGetVipGuestsLiveFallback:
    """get_vip_guests falls back to a live reservations query when no brief exists.

    Fresh-account / open-source safety: before the first roll-forward generates
    a brief, the tool must not report a false "no VIP arrivals" - it queries the
    reservations arrival-date index for that date's VIP-tier guests.
    """

    def test_falls_back_to_live_vip_arrivals_when_no_brief(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        module, mock_resource = tools
        # No brief for the date.
        mock_resource.Table.return_value.get_item.return_value = {}
        # Reservations arriving today: 2 VIP tiers + 1 non-VIP + 1 cancelled VIP.
        mock_resource.Table.return_value.query.return_value = {
            "Items": [
                {"guestId": "G1", "guestName": "A Platinum", "loyaltyTier": "PLATINUM",
                 "roomNumber": "1105", "roomType": "KING", "status": "CONFIRMED",
                 "estimatedArrival": "2026-09-04T18:00:00-05:00"},
                {"guestId": "G2", "guestName": "B Ambassador", "loyaltyTier": "AMBASSADOR",
                 "roomNumber": "2401", "roomType": "SUITE", "status": "CONFIRMED",
                 "estimatedArrival": "2026-09-04T14:00:00-05:00"},
                {"guestId": "G3", "guestName": "C Member", "loyaltyTier": "MEMBER",
                 "roomNumber": "300", "roomType": "KING", "status": "CONFIRMED"},
                {"guestId": "G4", "guestName": "D Cancelled", "loyaltyTier": "TITANIUM",
                 "roomNumber": "400", "roomType": "KING", "status": "CANCELLED"},
            ]
        }

        result = module.get_vip_guests("ALOHA-CHI-001", {"date": "2026-09-04"})

        assert result["status"] == "success"
        data = result["data"]
        assert data["source"] == "live"
        # Only the 2 non-cancelled VIP-tier guests are returned.
        assert data["vipCount"] == 2
        names = [g["guestName"] for g in data["guests"]]
        assert names == ["B Ambassador", "A Platinum"]  # AMBASSADOR ranks above PLATINUM
        # Non-VIP (MEMBER) and cancelled TITANIUM excluded.
        assert "C Member" not in names and "D Cancelled" not in names

    def test_live_fallback_empty_when_no_vip_arrivals(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        module, mock_resource = tools
        mock_resource.Table.return_value.get_item.return_value = {}
        mock_resource.Table.return_value.query.return_value = {
            "Items": [
                {"guestId": "G3", "guestName": "C Member", "loyaltyTier": "MEMBER",
                 "roomNumber": "300", "status": "CONFIRMED"},
            ]
        }

        result = module.get_vip_guests("ALOHA-CHI-001", {"date": "2026-09-04"})

        assert result["status"] == "success"
        assert result["data"]["vipCount"] == 0
        assert result["data"]["guests"] == []
        assert result["data"]["source"] == "live"

    def test_brief_present_takes_precedence_over_live(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        module, mock_resource = tools
        # A curated brief exists -> use it, do not fall back.
        mock_resource.Table.return_value.get_item.return_value = {
            "Item": {
                "propertyId": "ALOHA-CHI-001",
                "briefDate": "2026-09-04",
                "vipArrivals": [
                    {"guestName": "Brief Guest", "loyaltyTier": "PLATINUM",
                     "sensitiveNotes": "should be stripped"},
                ],
            }
        }

        result = module.get_vip_guests("ALOHA-CHI-001", {"date": "2026-09-04"})

        assert result["data"]["source"] == "brief"
        assert result["data"]["vipCount"] == 1
        # sensitiveNotes stripped.
        assert "sensitiveNotes" not in result["data"]["guests"][0]
        # query (live fallback) must NOT have been called.
        mock_resource.Table.return_value.query.assert_not_called()



    def test_live_fallback_dedupes_by_guest_and_caps(
        self, tools: Tuple[ModuleType, MagicMock]
    ) -> None:
        """Live fallback returns one entry per guest, capped at MAX_VIP_ARRIVALS_FALLBACK.

        The seed reuses a small VIP name pool across many reservations, so a raw
        arrival-date query returns the same guest on many bookings. The fallback
        must match the curated brief's contract: unique guests, capped.
        """
        module, mock_resource = tools
        mock_resource.Table.return_value.get_item.return_value = {}
        # 3 bookings for G1 (dup) + 10 distinct other VIP guests = should
        # dedupe G1 to one and cap the total at the fallback max (7).
        items = [
            {"guestId": "G1", "guestName": "Repeat Guest", "loyaltyTier": "PLATINUM",
             "roomNumber": str(700 + i), "status": "CONFIRMED"}
            for i in range(3)
        ] + [
            {"guestId": f"G{n}", "guestName": f"Guest {n}", "loyaltyTier": "TITANIUM",
             "roomNumber": str(800 + n), "status": "CONFIRMED"}
            for n in range(2, 12)
        ]
        mock_resource.Table.return_value.query.return_value = {"Items": items}

        result = module.get_vip_guests("ALOHA-MIA-001", {"date": "2026-09-04"})

        guests = result["data"]["guests"]
        assert result["data"]["source"] == "live"
        # Capped at the fallback max.
        assert result["data"]["vipCount"] == module.MAX_VIP_ARRIVALS_FALLBACK
        assert len(guests) == module.MAX_VIP_ARRIVALS_FALLBACK
        # G1 appears at most once (deduped by guestId).
        g1_count = sum(1 for g in guests if g["guestId"] == "G1")
        assert g1_count <= 1
