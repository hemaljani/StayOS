"""Unit tests for the Ops tab shaping (``GET /ops``).

Verifies that ``shape_ops`` composes the facility summary, OOO room cards joined
with their work-order status, and the group-checkout summary, and that every
tool call is scoped by ``propertyId``.
"""

from __future__ import annotations

from pulse.ops_read.ops import shape_ops
from tests.ops_read.conftest import RecordingToolCaller, ok

_PID = "ALOHA-CHI-001"


def _ops_results() -> dict[str, object]:
    """Build canned occupancy/room-status/work-order results for the Ops tab."""
    return {
        "get_occupancy": ok(
            {
                "date": "2026-08-18",
                "occupancyPct": 82,
                "arrivalsTotal": 45,
                "departuresTotal": 38,
                "confirmedReservations": 40,
                "availableRooms": 12,
            }
        ),
        "get_room_status": ok(
            {
                "oooCount": 2,
                "rooms": [
                    {
                        "roomNumber": "204",
                        "roomType": "KING",
                        "status": "OOO",
                        "floor": 2,
                        "view": "city",
                        "isPremiumRoom": False,
                        "currentWorkOrderId": "WO-100",
                    },
                    {
                        "roomNumber": "512",
                        "roomType": "SUITE",
                        "status": "MAINTENANCE",
                        "floor": 5,
                        "view": "lake",
                        "isPremiumRoom": True,
                        "currentWorkOrderId": None,
                    },
                ],
            }
        ),
        "get_work_orders": ok(
            {
                "totalCount": 2,
                "workOrders": [
                    {
                        "workOrderId": "WO-100",
                        "roomNumber": "204",
                        "status": "IN_PROGRESS",
                        "priority": "HIGH",
                        "issueType": "HVAC",
                        "assignedTo": "tech-7",
                        "createdAt": "2026-08-18T02:00:00Z",
                        "estimatedResolutionHours": 4,
                    },
                    {
                        "workOrderId": "WO-200",
                        "roomNumber": "512",
                        "status": "OPEN",
                        "priority": "MEDIUM",
                        "issueType": "PLUMBING",
                    },
                ],
            }
        ),
    }


def test_shape_ops_composes_facility_summary() -> None:
    """Facility summary carries occupancy, arrivals/departures, OOO + WO counts."""
    caller = RecordingToolCaller(_ops_results())

    result = shape_ops(_PID, caller)

    facility = result["facility"]
    assert facility["occupancyPct"] == 82
    assert facility["arrivalsTotal"] == 45
    assert facility["departuresTotal"] == 38
    assert facility["confirmedReservations"] == 40
    assert facility["availableRooms"] == 12
    assert facility["oooCount"] == 2
    assert facility["openWorkOrders"] == 2
    assert result["propertyId"] == _PID
    assert result["date"] == "2026-08-18"


def test_shape_ops_joins_ooo_rooms_with_work_orders() -> None:
    """Each OOO card joins its work order via currentWorkOrderId, else roomNumber."""
    caller = RecordingToolCaller(_ops_results())

    result = shape_ops(_PID, caller)

    cards = {card["roomNumber"]: card for card in result["oooRooms"]}
    assert set(cards) == {"204", "512"}

    # Room 204 links via currentWorkOrderId -> WO-100.
    wo_204 = cards["204"]["workOrder"]
    assert wo_204["workOrderId"] == "WO-100"
    assert wo_204["status"] == "IN_PROGRESS"
    assert wo_204["priority"] == "HIGH"

    # Room 512 has no currentWorkOrderId, so it falls back to a roomNumber match.
    wo_512 = cards["512"]["workOrder"]
    assert wo_512["workOrderId"] == "WO-200"
    assert wo_512["status"] == "OPEN"
    assert cards["512"]["isPremiumRoom"] is True


def test_shape_ops_ooo_card_without_work_order_is_none() -> None:
    """An OOO room with no matching work order carries workOrder=None."""
    results = _ops_results()
    results["get_work_orders"] = ok({"totalCount": 0, "workOrders": []})
    caller = RecordingToolCaller(results)

    result = shape_ops(_PID, caller)

    assert all(card["workOrder"] is None for card in result["oooRooms"])


def test_shape_ops_composes_group_checkout_summary() -> None:
    """Group-checkout summary is derived from occupancy departures/availability."""
    caller = RecordingToolCaller(_ops_results())

    result = shape_ops(_PID, caller)

    group_checkout = result["groupCheckout"]
    assert group_checkout["departuresTotal"] == 38
    assert group_checkout["availableRooms"] == 12
    assert group_checkout["confirmedReservations"] == 40


def test_shape_ops_scopes_every_tool_call_by_property_id() -> None:
    """All three Ops tool calls are scoped with the correct propertyId."""
    caller = RecordingToolCaller(_ops_results())

    shape_ops(_PID, caller)

    called = {name for name, _args in caller.calls}
    assert called == {"get_occupancy", "get_room_status", "get_work_orders"}
    assert all(pid == _PID for pid in caller.property_ids())
