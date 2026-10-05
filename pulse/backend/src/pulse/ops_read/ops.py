"""Shape occupancy/room/work-order results for the PWA Ops tab.

The Ops tab (Component 6) renders a facility summary, out-of-order (OOO) room
cards annotated with their work-order status, and a group-checkout summary. This
module composes that view from three shared Gateway tools, all scoped to the
caller's property server-side:

    * ``get_occupancy``   -> the facility summary + group-checkout numbers.
    * ``get_room_status`` -> the OOO/maintenance rooms.
    * ``get_work_orders`` -> the open/in-progress work orders, joined onto each
      OOO room by the room's ``currentWorkOrderId`` (falling back to a
      ``roomNumber`` match) so each card shows its work-order status.

The single dependency is the :data:`~pulse.ops_read.gateway.ToolCaller` seam, so
this shaping is fully unit-testable with an in-memory fake and never opens a
network connection.
"""

from __future__ import annotations

from typing import Any, Optional

from pulse.common.logging import get_logger
from pulse.ops_read.gateway import ToolCaller, tool_data

logger = get_logger("pulse-ops-read")

# The Gateway tools the Ops tab reads from.
OCCUPANCY_TOOL_NAME = "get_occupancy"
ROOM_STATUS_TOOL_NAME = "get_room_status"
WORK_ORDERS_TOOL_NAME = "get_work_orders"


def _shape_work_order(work_order: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Project a work-order item onto the fields the OOO card needs.

    Args:
        work_order: The matched work-order item, or ``None`` when the OOO room
            has no linked work order.

    Returns:
        The projected work-order card fields, or ``None`` when there is no
        matching work order.
    """
    if not work_order:
        return None
    return {
        "workOrderId": work_order.get("workOrderId"),
        "status": work_order.get("status"),
        "priority": work_order.get("priority"),
        "issueType": work_order.get("issueType"),
        "assignedTo": work_order.get("assignedTo"),
        "createdAt": work_order.get("createdAt"),
        "estimatedResolutionHours": work_order.get("estimatedResolutionHours"),
    }


def _index_work_orders(
    work_orders: list[Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Index work orders by id and by room number for OOO-room joining.

    Args:
        work_orders: The work-order items from ``get_work_orders``.

    Returns:
        A ``(by_id, by_room)`` tuple of lookups. ``by_room`` keeps the first
        work order seen per room number.
    """
    by_id: dict[str, dict[str, Any]] = {}
    by_room: dict[str, dict[str, Any]] = {}
    for work_order in work_orders:
        if not isinstance(work_order, dict):
            continue
        work_order_id = work_order.get("workOrderId")
        if work_order_id:
            by_id[str(work_order_id)] = work_order
        room_number = work_order.get("roomNumber")
        if room_number:
            by_room.setdefault(str(room_number), work_order)
    return by_id, by_room


def _match_work_order(
    room: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    by_room: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find the work order linked to an OOO room.

    Prefers the room's ``currentWorkOrderId`` link (the authoritative pointer
    set when a work order takes a room OOO), then falls back to matching by
    ``roomNumber``.

    Args:
        room: An OOO/maintenance room item from ``get_room_status``.
        by_id: Work orders indexed by ``workOrderId``.
        by_room: Work orders indexed by ``roomNumber``.

    Returns:
        The matched work-order item, or ``None`` when none matches.
    """
    current_id = room.get("currentWorkOrderId")
    if current_id and str(current_id) in by_id:
        return by_id[str(current_id)]
    room_number = room.get("roomNumber")
    if room_number and str(room_number) in by_room:
        return by_room[str(room_number)]
    return None


def _shape_ooo_rooms(
    ooo_rooms: list[Any], work_orders: list[Any]
) -> list[dict[str, Any]]:
    """Build OOO room cards, joining each room with its work-order status.

    Args:
        ooo_rooms: The OOO/maintenance rooms from ``get_room_status``.
        work_orders: The work orders from ``get_work_orders``.

    Returns:
        The OOO room cards, each with a nested ``workOrder`` (or ``None``).
    """
    by_id, by_room = _index_work_orders(work_orders)
    cards: list[dict[str, Any]] = []
    for room in ooo_rooms:
        if not isinstance(room, dict):
            continue
        matched = _match_work_order(room, by_id, by_room)
        cards.append(
            {
                "roomNumber": room.get("roomNumber"),
                "roomType": room.get("roomType"),
                "status": room.get("status"),
                "floor": room.get("floor"),
                "view": room.get("view"),
                "isPremiumRoom": room.get("isPremiumRoom"),
                "workOrder": _shape_work_order(matched),
            }
        )
    return cards


def shape_ops(property_id: str, tool_caller: ToolCaller) -> dict[str, Any]:
    """Assemble the ``GET /ops`` response from the occupancy/room/WO tools.

    Calls ``get_occupancy``, ``get_room_status``, and ``get_work_orders`` scoped
    to ``property_id`` and composes the facility summary, OOO room cards (each
    annotated with its joined work-order status), and the group-checkout summary.

    Args:
        property_id: The property to scope every tool call to (server-side).
        tool_caller: The Gateway tool-call seam.

    Returns:
        The Ops response body::

            {
              "propertyId": str,
              "date": str | None,
              "facility": {occupancyPct, arrivalsTotal, departuresTotal,
                           confirmedReservations, availableRooms, oooCount,
                           openWorkOrders},
              "oooRooms": [ {roomNumber, roomType, status, floor, view,
                             isPremiumRoom, workOrder: {...} | None} ],
              "groupCheckout": {departuresTotal, availableRooms,
                                confirmedReservations}
            }

    Raises:
        OpsReadFailure: If any Gateway tool is unavailable (propagated from
            :func:`~pulse.ops_read.gateway.tool_data`).
    """
    occupancy = tool_data(
        tool_caller(OCCUPANCY_TOOL_NAME, {"propertyId": property_id}),
        OCCUPANCY_TOOL_NAME,
    )
    room_status = tool_data(
        tool_caller(ROOM_STATUS_TOOL_NAME, {"propertyId": property_id}),
        ROOM_STATUS_TOOL_NAME,
    )
    work_orders_data = tool_data(
        tool_caller(WORK_ORDERS_TOOL_NAME, {"propertyId": property_id}),
        WORK_ORDERS_TOOL_NAME,
    )

    ooo_rooms = room_status.get("rooms") or []
    work_orders = work_orders_data.get("workOrders") or []
    ooo_cards = _shape_ooo_rooms(ooo_rooms, work_orders)

    facility = {
        "occupancyPct": occupancy.get("occupancyPct", 0),
        "arrivalsTotal": occupancy.get("arrivalsTotal", 0),
        "departuresTotal": occupancy.get("departuresTotal", 0),
        "confirmedReservations": occupancy.get("confirmedReservations", 0),
        "availableRooms": occupancy.get("availableRooms", 0),
        "oooCount": room_status.get("oooCount", len(ooo_rooms)),
        "openWorkOrders": work_orders_data.get("totalCount", len(work_orders)),
    }
    group_checkout = {
        "departuresTotal": occupancy.get("departuresTotal", 0),
        "availableRooms": occupancy.get("availableRooms", 0),
        "confirmedReservations": occupancy.get("confirmedReservations", 0),
    }

    logger.info(
        "Shaped Ops response",
        extra={
            "property_id": property_id,
            "oooCount": facility["oooCount"],
            "openWorkOrders": facility["openWorkOrders"],
        },
    )
    return {
        "propertyId": property_id,
        "date": occupancy.get("date"),
        "facility": facility,
        "oooRooms": ooo_cards,
        "groupCheckout": group_checkout,
    }


__all__ = [
    "OCCUPANCY_TOOL_NAME",
    "ROOM_STATUS_TOOL_NAME",
    "WORK_ORDERS_TOOL_NAME",
    "shape_ops",
]
