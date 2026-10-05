"""Unit tests for per-alert-type fact gathering (Gateway tools -> context).

Verifies the deterministic orchestration assembles a correct
``SituationContext`` from mocked Gateway tool results, that every tool call is
scoped with the correct ``propertyId``, and that LUMI loyalty tiers are
reconciled into the PULSE vocabulary so ``build_walk_strategy`` trusts the
tool's walkable selection.
"""

from __future__ import annotations

from situation import (
    LUMI_TO_PULSE_LOYALTY_TIER,
    build_ooo_cluster_context,
    build_premium_cancellation_context,
    build_situation_context,
    build_vip_checkin_context,
    build_walk_risk_context,
)

from pulse.common.models import AlertType
from tests.triage_agent.conftest import RecordingToolCaller

_PID = "ALOHA-CHI-001"


def test_walk_risk_context_derives_shortfall_and_reconciles_tiers() -> None:
    """Walk Risk context derives the shortfall and maps LUMI tiers to PULSE."""
    caller = RecordingToolCaller(
        {
            "get_occupancy": {"arrivals": 8, "availableRooms": 2, "date": "2026-08-17"},
            "get_walkable_guests": [
                {"guestId": "G-1", "reservationId": "R-1", "loyaltyTier": "PLATINUM"},
                {"guestId": "G-2", "reservationId": "R-2", "loyaltyTier": "TITANIUM"},
            ],
        }
    )

    context = build_walk_risk_context(_PID, caller)

    # No confirmedReservations in this fixture -> falls back to arrivals:
    # shortfall = arrivals (8) - available (2) = 6.
    assert context.room_shortfall == 6
    assert context.stay_dates == ("2026-08-17", "2026-08-17")
    # LUMI tiers reconciled into the PULSE vocabulary loyalty_rank understands.
    tiers = [guest["loyaltyTier"] for guest in context.confirmed_guests]
    assert tiers == [
        LUMI_TO_PULSE_LOYALTY_TIER["PLATINUM"],
        LUMI_TO_PULSE_LOYALTY_TIER["TITANIUM"],
    ]
    # Option B: no cross-city sister-property lookup is attached.
    assert context.sister_property_lookup is None


def test_walk_risk_shortfall_prefers_confirmed_reservations() -> None:
    """Shortfall uses confirmedReservations - availableRooms when present.

    arrivalsTotal is a smaller same-day figure; the walk-risk demand is the firm
    confirmed booking count. This is what made the deterministic strategy read a
    0 shortfall (and go empty) when it keyed off arrivals instead.
    """
    caller = RecordingToolCaller(
        {
            "get_occupancy": {
                "arrivals": 175,
                "confirmedReservations": 450,
                "availableRooms": 444,
                "date": "2026-08-21",
            },
            "get_walkable_guests": [
                {"guestId": "G-1", "reservationId": "R-1", "loyaltyTier": "PLATINUM"},
            ],
        }
    )

    context = build_walk_risk_context(_PID, caller)

    # confirmedReservations (450) - availableRooms (444) = 6, not arrivals-based 0.
    assert context.room_shortfall == 6
    # And that shortfall is threaded to the walkable-guests tool.
    called = {name: args for name, args in caller.calls}
    assert called["get_walkable_guests"]["shortfall"] == 6


def test_walk_risk_context_passes_property_id_and_shortfall_to_tools() -> None:
    """Every Walk Risk tool call is scoped by propertyId; shortfall is threaded.

    Option B: the sister-property-availability tool is no longer called.
    """
    caller = RecordingToolCaller(
        {
            "get_occupancy": {"arrivals": 5, "availableRooms": 1, "date": "2026-08-17"},
            "get_walkable_guests": [],
        }
    )

    build_walk_risk_context(_PID, caller)

    called = {name: args for name, args in caller.calls}
    assert set(called) == {
        "get_occupancy",
        "get_walkable_guests",
    }
    # Every call carries the correct propertyId (server-side scope).
    assert all(pid == _PID for pid in caller.property_ids())
    # The derived shortfall (4) is passed to the walkable tool.
    assert called["get_walkable_guests"]["shortfall"] == 4


def test_ooo_context_selects_required_type_and_maps_candidates() -> None:
    """OOO context picks the dominant OOO room type and maps move candidates."""
    caller = RecordingToolCaller(
        {
            "get_room_status": [
                {"roomId": "101", "roomType": "KING", "status": "OOO"},
                {"roomId": "102", "roomType": "KING", "status": "OOO"},
                {"roomId": "103", "roomType": "SUITE", "status": "OOO"},
            ],
            "get_room_move_candidates": [
                {"roomId": "201", "roomType": "KING", "suitability": 0.9},
                {"roomId": "202", "roomType": "KING"},
            ],
        }
    )

    context = build_ooo_cluster_context(_PID, caller)

    # KING is the most common OOO room type.
    assert context.required_room_type == "KING"
    # The move-candidate tool is scoped to that room type.
    move_args = dict(caller.calls)["get_room_move_candidates"]
    assert move_args["roomType"] == "KING"
    assert move_args["propertyId"] == _PID
    # Candidates are marked available for the range with a suitability score.
    assert len(context.replacement_candidates) == 2
    assert context.replacement_candidates[0]["availableForRange"] is True
    assert context.replacement_candidates[0]["suitability"] == 0.9
    assert all(pid == _PID for pid in caller.property_ids())


# ---------------------------------------------------------------------------
# INFO advisory-opportunity contexts (Premium Cancellation, VIP Check-In)
# ---------------------------------------------------------------------------


def test_premium_cancellation_context_gathers_revenue_and_occupancy() -> None:
    """Premium Cancellation context pulls revenue + occupancy, scoped by property."""
    caller = RecordingToolCaller(
        {
            "get_revenue": {
                "adr": 320,
                "revpar": 210,
                "currency": "EUR",
                "vsBudget": -12,
            },
            "get_occupancy": {"occupancyPct": 78, "availableRooms": 14},
        }
    )

    context = build_premium_cancellation_context(_PID, caller)

    # Both revenue and occupancy facts are carried under extra for the prompt.
    assert context.extra["revenue"]["adr"] == 320
    assert context.extra["occupancy"]["availableRooms"] == 14
    # The property's currency is lifted from the revenue tool result.
    assert context.currency == "EUR"
    # Both tools were called and every call is property-scoped.
    assert set(dict(caller.calls)) == {"get_revenue", "get_occupancy"}
    assert all(pid == _PID for pid in caller.property_ids())


def test_premium_cancellation_context_defaults_currency_when_absent() -> None:
    """Currency falls back to USD when the revenue tool omits it."""
    caller = RecordingToolCaller(
        {
            "get_revenue": {"adr": 0, "revpar": 0},
            "get_occupancy": {"occupancyPct": 0, "availableRooms": 0},
        }
    )

    context = build_premium_cancellation_context(_PID, caller)

    assert context.currency == "USD"


def test_vip_checkin_context_gathers_vip_guests_and_occupancy() -> None:
    """VIP Check-In context pulls the VIP guest list + occupancy, property-scoped."""
    caller = RecordingToolCaller(
        {
            "get_vip_guests": {
                "guests": [
                    {
                        "guestId": "G-9",
                        "loyaltyTier": "AMBASSADOR",
                        "roomType": "SUITE",
                        "specialRequests": "High floor, sparkling water",
                    }
                ]
            },
            "get_occupancy": {"occupancyPct": 62, "availableRooms": 20},
        }
    )

    context = build_vip_checkin_context(_PID, caller)

    # The VIP guest list is surfaced under extra for the model to personalize.
    assert len(context.extra["vipGuests"]) == 1
    assert context.extra["vipGuests"][0]["guestId"] == "G-9"
    assert context.extra["occupancy"]["availableRooms"] == 20
    assert set(dict(caller.calls)) == {"get_vip_guests", "get_occupancy"}
    assert all(pid == _PID for pid in caller.property_ids())


def test_build_situation_context_dispatches_info_types() -> None:
    """build_situation_context routes both INFO types to their builders."""
    premium_caller = RecordingToolCaller(
        {
            "get_revenue": {"adr": 300, "currency": "USD"},
            "get_occupancy": {"availableRooms": 5},
        }
    )
    premium = build_situation_context(
        AlertType.PREMIUM_CANCELLATION, _PID, premium_caller
    )
    assert premium.property_id == _PID
    assert "revenue" in premium.extra

    vip_caller = RecordingToolCaller(
        {
            "get_vip_guests": {"guests": []},
            "get_occupancy": {"availableRooms": 5},
        }
    )
    vip = build_situation_context(AlertType.VIP_CHECKIN, _PID, vip_caller)
    assert vip.property_id == _PID
    assert "vipGuests" in vip.extra
