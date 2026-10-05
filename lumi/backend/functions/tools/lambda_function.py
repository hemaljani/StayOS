"""Tool Lambda function for AgentCore Gateway tool invocations.

Implements read-only hotel operations tools that are invoked by the
AgentCore Gateway when an agent calls a tool via MCP. Each tool queries
DynamoDB using the same access patterns as the voice agent tool_handlers.py.

Two tool families are registered here:
    * The original 5 LUMI tools (occupancy, revenue, VIP guests, room status,
      work orders) consumed by the LUMI chat agent.
    * 3 read-only PULSE tools (sister-property availability, walkable-guest
      selection, room-move candidates) consumed by the PULSE Triage Agent.
      These are additive and share this single Lambda target (Decision 7:
      one shared StayOS Gateway target rather than a second Lambda).

This is a synchronous Lambda (no asyncio) - boto3 calls are made directly
since Lambda runs each invocation in a single thread. The Gateway sends a
JSON payload with tool_name and tool_input, and the function returns a
structured response with status and data.

Role in project: Registered as a Lambda target on the AgentCore Gateway.
The chat agent discovers and calls these tools via MCP; the Gateway invokes
this Lambda to execute the actual DynamoDB queries.
"""

import os
import time
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3
from aws_lambda_powertools import Logger, Tracer
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import ClientError

# Powertools Logger and Tracer at module level (service name: stayos-tools)
logger = Logger(service="stayos-tools")
tracer = Tracer(service="stayos-tools")

# Dataset table names from environment variables
RESERVATIONS_TABLE_NAME: str = os.environ.get("RESERVATIONS_TABLE_NAME", "")
ROOMS_TABLE_NAME: str = os.environ.get("ROOMS_TABLE_NAME", "")
GUESTS_TABLE_NAME: str = os.environ.get("GUESTS_TABLE_NAME", "")
REVENUES_TABLE_NAME: str = os.environ.get("REVENUES_TABLE_NAME", "")
WORK_ORDERS_TABLE_NAME: str = os.environ.get("WORK_ORDERS_TABLE_NAME", "")
BRIEFS_TABLE_NAME: str = os.environ.get("BRIEFS_TABLE_NAME", "")

# Optional comma-separated list of every propertyId in the estate, used by
# get_sister_property_availability to enumerate candidate sister properties.
# When unset, the estate is discovered by scanning the rooms table (a
# prototype-friendly fallback since the pilot estate is only a handful of
# properties). Sourced from an env var to avoid hardcoding resource ids.
ESTATE_PROPERTY_IDS_RAW: str = os.environ.get("ESTATE_PROPERTY_IDS", "")

# VIP loyalty tiers that qualify for VIP arrival alerts
VIP_TIERS: Set[str] = {"AMBASSADOR", "TITANIUM", "PLATINUM"}

# Cap for the live VIP-arrivals fallback in get_vip_guests, mirroring the
# curated brief's MAX_VIP_ARRIVALS so the fallback is a tight, GM-useful list
# (the seed tags most reservations VIP-tier, so an uncapped query is a firehose).
MAX_VIP_ARRIVALS_FALLBACK: int = 7

# Internal composite keys to strip from responses (GSI implementation detail)
INTERNAL_KEYS: Set[str] = {"statusRoomNumber", "statusCreatedAt"}

# Room status treated as "ready / available for reassignment". The LUMI
# operational dataset uses AVAILABLE (there is no distinct "Ready" status), so
# PULSE room-move and sister-availability facts are derived from AVAILABLE rooms.
AVAILABLE_ROOM_STATUS: str = "AVAILABLE"

# Reservation status that represents a firm, not-yet-arrived booking. These are
# the guests eligible to be walked when a night is oversold (PULSE Walk Risk).
CONFIRMED_RESERVATION_STATUS: str = "CONFIRMED"

# Loyalty-tier ranking for the LUMI dataset (higher rank = more elite). A guest
# whose rank is at or below the configured protection tier's rank is walkable
# (mirrors pulse.triage.context.loyalty_rank semantics). Note: the LUMI seed
# data uses PLATINUM/TITANIUM/AMBASSADOR, which differ from the tier names in
# the PULSE requirements glossary - the triage layer maps these via its seams.
LOYALTY_TIER_RANK: Dict[str, int] = {
    "PLATINUM": 0,
    "TITANIUM": 1,
    "AMBASSADOR": 2,
}

# Default protection tier when a caller does not supply one. Defaults to the
# least-elite tier so only the lowest-loyalty guests are considered walkable
# (the conservative, guest-protective choice).
DEFAULT_LOYALTY_PROTECTION_TIER: str = "PLATINUM"

# Module-level DynamoDB resource with standard retry config (connection reuse)
_dynamodb_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)
_dynamodb_resource = boto3.resource("dynamodb", config=_dynamodb_config)


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------


def _decimal_to_native(value: Any) -> Any:
    """Convert DynamoDB Decimal values to native Python int or float.

    DynamoDB returns all numbers as Decimal objects which are not
    JSON-serializable. This recursively converts them for tool results.

    Args:
        value: Any value that may be or contain Decimal objects.

    Returns:
        Value with all Decimals converted to int (if integer) or float.
    """
    if isinstance(value, Decimal):
        if value == int(value):
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_native(item) for item in value]
    return value


def _strip_internal_keys(item: Dict[str, Any]) -> Dict[str, Any]:
    """Remove internal composite keys from a DynamoDB item before returning.

    Composite sort keys like statusRoomNumber and statusCreatedAt are
    implementation details of the GSI design, not meaningful to callers.

    Args:
        item: A DynamoDB item dict.

    Returns:
        Item with internal composite keys removed.
    """
    return {k: v for k, v in item.items() if k not in INTERNAL_KEYS}


def _today_iso() -> str:
    """Return today's date as an ISO 8601 string (YYYY-MM-DD).

    Returns:
        Today's date string in ISO format.
    """
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# DynamoDB Query Functions
# ---------------------------------------------------------------------------


def _get_revenue_item(
    property_id: str,
    date_str: str,
) -> Optional[Dict[str, Any]]:
    """Retrieve a revenue record from the revenues table by composite key.

    Uses GetItem with the (propertyId, date) composite key for
    single-digit millisecond retrieval of the daily revenue snapshot.

    Args:
        property_id: The property identifier (partition key).
        date_str: ISO date string e.g. "2025-01-15" (sort key).

    Returns:
        Revenue item dict or None if no record exists.
    """
    table = _dynamodb_resource.Table(REVENUES_TABLE_NAME)
    response = table.get_item(Key={"propertyId": property_id, "date": date_str})
    return response.get("Item")


def _query_ooo_maintenance_rooms(property_id: str) -> List[Dict[str, Any]]:
    """Query rooms with OOO or MAINTENANCE status from the rooms table.

    Uses the propertyId-statusRoomNumber-index GSI with begins_with on
    the composite sort key to find rooms currently out of order or under
    maintenance.

    Args:
        property_id: The property identifier (GSI partition key).

    Returns:
        Combined list of OOO and MAINTENANCE room items.
    """
    table = _dynamodb_resource.Table(ROOMS_TABLE_NAME)
    rooms: List[Dict[str, Any]] = []

    # Query for OOO rooms (statusRoomNumber begins with "OOO#")
    ooo_kwargs: Dict[str, Any] = {
        "IndexName": "propertyId-statusRoomNumber-index",
        "KeyConditionExpression": (
            Key("propertyId").eq(property_id)
            & Key("statusRoomNumber").begins_with("OOO#")
        ),
    }
    while True:
        ooo_response = table.query(**ooo_kwargs)
        rooms.extend(ooo_response.get("Items", []))
        if "LastEvaluatedKey" not in ooo_response:
            break
        ooo_kwargs["ExclusiveStartKey"] = ooo_response["LastEvaluatedKey"]

    # Query for MAINTENANCE rooms (statusRoomNumber begins with "MAINTENANCE#")
    maint_kwargs: Dict[str, Any] = {
        "IndexName": "propertyId-statusRoomNumber-index",
        "KeyConditionExpression": (
            Key("propertyId").eq(property_id)
            & Key("statusRoomNumber").begins_with("MAINTENANCE#")
        ),
    }
    while True:
        maint_response = table.query(**maint_kwargs)
        rooms.extend(maint_response.get("Items", []))
        if "LastEvaluatedKey" not in maint_response:
            break
        maint_kwargs["ExclusiveStartKey"] = maint_response["LastEvaluatedKey"]

    return rooms


def _query_work_orders_by_status(
    property_id: str,
    status_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query work orders from the work-orders table by status.

    Uses the propertyId-statusCreatedAt-index GSI with begins_with on
    the composite sort key. When no status filter is provided, queries
    both OPEN and IN_PROGRESS work orders.

    Args:
        property_id: The property identifier (GSI partition key).
        status_filter: Optional status to filter (OPEN or IN_PROGRESS).

    Returns:
        List of work order items matching the status filter.
    """
    table = _dynamodb_resource.Table(WORK_ORDERS_TABLE_NAME)
    work_orders: List[Dict[str, Any]] = []

    # Determine which statuses to query
    statuses_to_query: List[str] = []
    if status_filter and status_filter.upper() in ("OPEN", "IN_PROGRESS"):
        statuses_to_query = [status_filter.upper()]
    else:
        statuses_to_query = ["OPEN", "IN_PROGRESS"]

    for status_prefix in statuses_to_query:
        query_kwargs: Dict[str, Any] = {
            "IndexName": "propertyId-statusCreatedAt-index",
            "KeyConditionExpression": (
                Key("propertyId").eq(property_id)
                & Key("statusCreatedAt").begins_with(f"{status_prefix}#")
            ),
        }
        while True:
            response = table.query(**query_kwargs)
            work_orders.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return work_orders


# ---------------------------------------------------------------------------
# PULSE Query Helpers (read-only)
# ---------------------------------------------------------------------------


def _loyalty_rank(tier: Optional[str]) -> int:
    """Return the elite-ranking integer for a loyalty tier.

    Higher rank means a more elite tier. Unknown or missing tiers map to the
    lowest rank (0) so they are treated as least-elite (walkable).

    Args:
        tier: A loyalty tier name (e.g. "AMBASSADOR"), or None.

    Returns:
        The tier's integer rank, or 0 when the tier is unknown/absent.
    """
    if not tier:
        return 0
    return LOYALTY_TIER_RANK.get(tier.upper(), 0)


def _brand_prefix(property_id: str) -> str:
    """Extract the brand token from a propertyId (heuristic for "sister").

    PropertyIds follow a BRAND-CITY-NUMBER convention (e.g. "ALOHA-CHI-001"),
    so the leading segment identifies the brand. Sister properties are other
    properties sharing this brand token.

    Args:
        property_id: The property identifier.

    Returns:
        The leading brand segment, or the whole id when it has no delimiter.
    """
    return property_id.split("-", 1)[0] if "-" in property_id else property_id


def _discover_estate_property_ids() -> List[str]:
    """Return every propertyId in the estate for sister-property enumeration.

    Prefers the ESTATE_PROPERTY_IDS env var (comma-separated). When unset,
    falls back to scanning the rooms table and projecting only propertyId -
    acceptable for the small pilot estate, and avoids hardcoding ids. The
    projected scan is paginated to collect the full distinct set.

    Returns:
        Sorted list of distinct propertyId strings.
    """
    configured = [pid.strip() for pid in ESTATE_PROPERTY_IDS_RAW.split(",") if pid.strip()]
    if configured:
        return sorted(set(configured))

    table = _dynamodb_resource.Table(ROOMS_TABLE_NAME)
    property_ids: Set[str] = set()
    # ProjectionExpression limits the scan payload to the partition key only.
    scan_kwargs: Dict[str, Any] = {"ProjectionExpression": "propertyId"}
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            pid = item.get("propertyId")
            if pid:
                property_ids.add(str(pid))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return sorted(property_ids)


def _query_available_rooms(
    property_id: str,
    room_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query rooms that are AVAILABLE (ready for assignment) for a property.

    Uses the propertyId-statusRoomNumber-index GSI with begins_with on the
    composite sort key to select AVAILABLE rooms. When room_type is supplied,
    a FilterExpression narrows the result to matching room types.

    Args:
        property_id: The property identifier (GSI partition key).
        room_type: Optional room type to match (e.g. "SUITE").

    Returns:
        List of AVAILABLE room items (optionally filtered by room type).
    """
    table = _dynamodb_resource.Table(ROOMS_TABLE_NAME)
    rooms: List[Dict[str, Any]] = []

    query_kwargs: Dict[str, Any] = {
        "IndexName": "propertyId-statusRoomNumber-index",
        "KeyConditionExpression": (
            Key("propertyId").eq(property_id)
            & Key("statusRoomNumber").begins_with(f"{AVAILABLE_ROOM_STATUS}#")
        ),
    }
    if room_type:
        # roomType is a non-key attribute, so it is applied as a post-read filter.
        query_kwargs["FilterExpression"] = Attr("roomType").eq(room_type)

    while True:
        response = table.query(**query_kwargs)
        rooms.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return rooms


def _query_confirmed_arrivals(
    property_id: str,
    arrival_date: str,
) -> List[Dict[str, Any]]:
    """Query CONFIRMED reservations arriving on a given date for a property.

    Uses the propertyId-arrivalDate-index GSI to select the property's
    arrivals for the date, then filters to CONFIRMED status (firm bookings
    not yet checked in) - the population eligible to be walked.

    Args:
        property_id: The property identifier (GSI partition key).
        arrival_date: ISO date string (YYYY-MM-DD) of the arrival night.

    Returns:
        List of CONFIRMED reservation items arriving on that date.
    """
    table = _dynamodb_resource.Table(RESERVATIONS_TABLE_NAME)
    reservations: List[Dict[str, Any]] = []

    query_kwargs: Dict[str, Any] = {
        "IndexName": "propertyId-arrivalDate-index",
        "KeyConditionExpression": (
            Key("propertyId").eq(property_id) & Key("arrivalDate").eq(arrival_date)
        ),
        # Only firm, not-yet-arrived bookings are walkable candidates.
        "FilterExpression": Attr("status").eq(CONFIRMED_RESERVATION_STATUS),
    }

    while True:
        response = table.query(**query_kwargs)
        reservations.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return reservations


def _query_vip_arrivals_live(
    property_id: str,
    arrival_date: str,
) -> List[Dict[str, Any]]:
    """Live fallback: VIP-tier arrivals for a date, direct from reservations.

    Used by :func:`get_vip_guests` when no curated brief exists for the date
    (fresh deploy before the first roll-forward, or a reference-date mismatch).
    Queries the propertyId-arrivalDate-index for the property's arrivals on the
    date, keeps only VIP loyalty tiers (:data:`VIP_TIERS`), and shapes each into
    the same fields the brief's ``vipArrivals`` entries use so the caller/UI is
    consistent. Read-only and property-scoped. ``sensitiveNotes`` is never
    included (it is not read from reservations here).

    Args:
        property_id: The property identifier (GSI partition key).
        arrival_date: ISO date string (YYYY-MM-DD) of the arrival night.

    Returns:
        List of VIP-arrival dicts (may be empty) sorted by loyalty rank then ETA.
    """
    table = _dynamodb_resource.Table(RESERVATIONS_TABLE_NAME)
    query_kwargs: Dict[str, Any] = {
        "IndexName": "propertyId-arrivalDate-index",
        "KeyConditionExpression": (
            Key("propertyId").eq(property_id) & Key("arrivalDate").eq(arrival_date)
        ),
    }

    vips: List[Dict[str, Any]] = []
    seen_guest_ids: set = set()
    while True:
        response = table.query(**query_kwargs)
        for reservation in response.get("Items", []):
            tier = str(reservation.get("loyaltyTier") or "").upper()
            if tier not in VIP_TIERS:
                continue
            # Skip cancelled bookings - they are not arriving.
            if str(reservation.get("status") or "").upper() == "CANCELLED":
                continue
            # Dedupe by guest: the seed reuses a small VIP name pool across many
            # reservations, so one guest can appear on several bookings for the
            # day. Match the brief's "unique VIP arrivals" contract - one entry
            # per guest (first booking seen).
            guest_id = str(reservation.get("guestId") or "")
            if guest_id and guest_id in seen_guest_ids:
                continue
            if guest_id:
                seen_guest_ids.add(guest_id)
            vips.append({
                "guestId": reservation.get("guestId"),
                "guestName": reservation.get("guestName"),
                "loyaltyTier": reservation.get("loyaltyTier"),
                "roomNumber": reservation.get("roomNumber"),
                "roomType": reservation.get("roomType"),
                "estimatedArrival": reservation.get("estimatedArrival"),
                "specialRequests": reservation.get("specialRequests"),
            })
        if "LastEvaluatedKey" not in response:
            break
        query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    # Highest-tier first (negate the rank, where higher rank = more elite),
    # then by ETA when present, for a stable, useful order.
    vips.sort(
        key=lambda v: (
            -_loyalty_rank(v.get("loyaltyTier")),
            str(v.get("estimatedArrival") or ""),
        )
    )
    # Cap to the same size the curated brief shows so the fallback is a tight,
    # GM-useful list rather than every VIP-tier booking for the day.
    return vips[:MAX_VIP_ARRIVALS_FALLBACK]


@tracer.capture_method
def get_occupancy(property_id: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Query occupancy metrics from the revenues table for a given date.

    Retrieves occupancy percentage, total arrivals, total departures,
    confirmed reservations, and available rooms from the daily revenue
    snapshot record.

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: Tool parameters containing optional 'date' field.

    Returns:
        Success dict with occupancy data fields.
    """
    date_str = tool_input.get("date") or _today_iso()

    # GetItem on revenues table with composite key (propertyId, date)
    item = _get_revenue_item(property_id, date_str)

    if not item:
        logger.info(
            "No revenue record found for date",
            extra={"property_id": property_id, "date": date_str},
        )
        return {
            "status": "success",
            "data": {
                "date": date_str,
                "occupancyPct": 0,
                "arrivalsTotal": 0,
                "departuresTotal": 0,
                "confirmedReservations": 0,
                "availableRooms": 0,
                "message": f"No occupancy data available for {date_str}",
            },
        }

    return {
        "status": "success",
        "data": _decimal_to_native({
            "date": date_str,
            "occupancyPct": item.get("occupancyPct", 0),
            "arrivalsTotal": item.get("arrivals", 0),
            "departuresTotal": item.get("departures", 0),
            "confirmedReservations": item.get("confirmedReservations", 0),
            "availableRooms": item.get("availableRooms", 0),
        }),
    }


@tracer.capture_method
def get_revenue(property_id: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Query revenue KPIs (ADR, RevPAR, comparisons) from the revenues table.

    Retrieves Average Daily Rate, Revenue Per Available Room, and
    period-over-period comparisons from the daily revenue snapshot.

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: Tool parameters with optional 'start_date' and 'end_date'.

    Returns:
        Success dict with revenue KPI fields.
    """
    start = tool_input.get("start_date") or _today_iso()
    end = tool_input.get("end_date") or start

    # For single-day query, use GetItem (range queries not supported in seed data)
    item = _get_revenue_item(property_id, start)

    if not item:
        logger.info(
            "No revenue record found for date",
            extra={"property_id": property_id, "date": start},
        )
        return {
            "status": "success",
            "data": {
                "date": start,
                "adr": 0,
                "revpar": 0,
                "currency": "USD",
                "vsLastWeek": 0,
                "vsBudget": 0,
                "vsYOY": 0,
                "message": f"No revenue data available for {start}",
            },
        }

    return {
        "status": "success",
        "data": _decimal_to_native({
            "date": start,
            "adr": item.get("adr", 0),
            "revpar": item.get("revpar", 0),
            "currency": item.get("currency", "USD"),
            "vsLastWeek": item.get("vsLastWeek", 0),
            "vsBudget": item.get("vsBudget", 0),
            "vsYOY": item.get("vsYOY", 0),
        }),
    }


@tracer.capture_method
def get_vip_guests(property_id: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Query VIP guest arrivals from the daily brief in the briefs table.

    Reads the pre-generated brief's vipArrivals array to ensure the tool
    returns the same VIP list that the frontend UI displays. Strips
    sensitiveNotes from each guest entry before returning.

    When no brief exists for the date (e.g. a fresh deploy before the first
    per-property roll-forward has generated today's brief, or a reference-date
    mismatch), it falls back to a LIVE query of the reservations table for that
    date's VIP-tier arrivals, so a missing brief degrades gracefully instead of
    reporting a false "no VIP arrivals". The fallback result is flagged with
    ``source: "live"`` so callers can tell it did not come from a curated brief.

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: Tool parameters with optional 'date' field.

    Returns:
        Success dict with VIP guest list from the brief (or a live fallback).
    """
    date_str = tool_input.get("date") or _today_iso()

    # Read the brief's vipArrivals from the briefs table
    briefs_table = _dynamodb_resource.Table(BRIEFS_TABLE_NAME)
    response = briefs_table.get_item(
        Key={"propertyId": property_id, "briefDate": date_str},
    )
    item = response.get("Item")

    if not item or not item.get("vipArrivals"):
        # No curated brief for this date -> fall back to a live reservations
        # query so we never report a false "no VIP arrivals" (e.g. before the
        # first roll-forward on a fresh deploy). Read-only, property-scoped.
        live_vips = _query_vip_arrivals_live(property_id, date_str)
        if live_vips:
            return {
                "status": "success",
                "data": _decimal_to_native({
                    "date": date_str,
                    "vipCount": len(live_vips),
                    "guests": live_vips,
                    "source": "live",
                }),
            }
        return {
            "status": "success",
            "data": {
                "date": date_str,
                "vipCount": 0,
                "guests": [],
                "source": "live",
                "message": f"No VIP arrivals found for {date_str}",
            },
        }

    vip_arrivals = item["vipArrivals"]

    # Strip sensitiveNotes from each guest (safety filter - never expose PII)
    for guest in vip_arrivals:
        guest.pop("sensitiveNotes", None)

    return {
        "status": "success",
        "data": _decimal_to_native({
            "date": date_str,
            "vipCount": len(vip_arrivals),
            "guests": vip_arrivals,
            "source": "brief",
        }),
    }


@tracer.capture_method
def get_room_status(property_id: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Query rooms currently out of order or in maintenance.

    Queries the rooms table GSI for OOO and MAINTENANCE status prefixes,
    returning room numbers, issue descriptions, and time in current state.
    Strips internal composite keys before returning.

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: Tool parameters (no additional fields used).

    Returns:
        Success dict with OOO/maintenance room list.
    """
    rooms = _query_ooo_maintenance_rooms(property_id)

    # Strip internal composite keys and convert Decimals
    cleaned_rooms = [
        _decimal_to_native(_strip_internal_keys(room)) for room in rooms
    ]

    return {
        "status": "success",
        "data": {
            "oooCount": len(cleaned_rooms),
            "rooms": cleaned_rooms,
        },
    }


@tracer.capture_method
def get_work_orders(property_id: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Query open and in-progress work orders for the property.

    Queries the work-orders table GSI for OPEN and/or IN_PROGRESS status
    prefixes, returning work order details with priority and age. Strips
    internal composite keys before returning.

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: Tool parameters with optional 'status' field.

    Returns:
        Success dict with work order list.
    """
    status_filter = tool_input.get("status")

    work_orders = _query_work_orders_by_status(property_id, status_filter)

    # Strip internal composite keys and convert Decimals
    cleaned_orders = [
        _decimal_to_native(_strip_internal_keys(order)) for order in work_orders
    ]

    return {
        "status": "success",
        "data": {
            "totalCount": len(cleaned_orders),
            "workOrders": cleaned_orders,
        },
    }


# ---------------------------------------------------------------------------
# PULSE Tool Functions (read-only, consumed by the Triage Agent)
# ---------------------------------------------------------------------------


@tracer.capture_method
def get_sister_property_availability(
    property_id: str,
    tool_input: Dict[str, Any],
) -> Dict[str, Any]:
    """Find sister properties with room availability for a stay range.

    Sister properties are other properties sharing this property's brand token
    (the leading propertyId segment - see _brand_prefix). For each sister, the
    count of AVAILABLE rooms (optionally matching roomType) is reported; only
    sisters with at least one available room are returned. Supports the PULSE
    Walk Risk sister-property lookup (Requirement 3.4).

    Note: the LUMI dataset models availability as a current room-status
    snapshot, not a per-date calendar, so startDate/endDate are echoed back for
    context but availability is derived from current AVAILABLE inventory.

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: Optional 'startDate', 'endDate', and 'roomType' fields.

    Returns:
        Success dict with the requested range and a list of sister properties.
    """
    start_date = tool_input.get("startDate")
    end_date = tool_input.get("endDate") or start_date
    room_type = tool_input.get("roomType")

    brand = _brand_prefix(property_id)
    estate = _discover_estate_property_ids()

    sister_properties: List[Dict[str, Any]] = []
    for candidate in estate:
        if candidate == property_id or _brand_prefix(candidate) != brand:
            continue
        available = _query_available_rooms(candidate, room_type)
        if not available:
            continue
        sister_properties.append(
            {"propertyId": candidate, "availableRooms": len(available)}
        )

    return {
        "status": "success",
        "data": _decimal_to_native({
            "startDate": start_date,
            "endDate": end_date,
            "roomType": room_type,
            "sisterProperties": sister_properties,
        }),
    }


@tracer.capture_method
def get_walkable_guests(
    property_id: str,
    tool_input: Dict[str, Any],
) -> Dict[str, Any]:
    """Select confirmed guests eligible to be walked, capped at the shortfall.

    Returns up to 'shortfall' CONFIRMED guests arriving on the given date whose
    loyalty rank is at or below the protection tier's rank, ordered
    lowest-loyalty first. Enforces the shortfall cap and the at-or-below-tier
    rule (PULSE Walk Risk, Requirements 3.3, 3.5, 3.6).

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: 'shortfall' (int, required), optional 'loyaltyProtectionTier'
            and 'arrivalDate' (defaults to today) fields.

    Returns:
        Success dict with the walkable guests and the applied shortfall cap.
    """
    cap = max(int(tool_input.get("shortfall", 0) or 0), 0)
    protection_tier = tool_input.get("loyaltyProtectionTier") or DEFAULT_LOYALTY_PROTECTION_TIER
    arrival_date = tool_input.get("arrivalDate") or _today_iso()
    protection_rank = _loyalty_rank(protection_tier)

    confirmed = _query_confirmed_arrivals(property_id, arrival_date)

    # Keep guests at or below the protection threshold, least-elite first.
    eligible = [
        reservation
        for reservation in confirmed
        if _loyalty_rank(reservation.get("loyaltyTier")) <= protection_rank
    ]
    eligible.sort(key=lambda reservation: _loyalty_rank(reservation.get("loyaltyTier")))
    selected = eligible[:cap]

    walkable_guests = [
        {
            "guestId": reservation.get("guestId"),
            "loyaltyTier": reservation.get("loyaltyTier"),
            "reservationId": reservation.get("reservationId"),
        }
        for reservation in selected
    ]

    return {
        "status": "success",
        "data": _decimal_to_native({
            "arrivalDate": arrival_date,
            "loyaltyProtectionTier": protection_tier,
            "walkableGuests": walkable_guests,
            "cappedAtShortfall": cap,
        }),
    }


@tracer.capture_method
def get_room_move_candidates(
    property_id: str,
    tool_input: Dict[str, Any],
) -> Dict[str, Any]:
    """List Ready rooms available for reassignment (e.g. a VIP room move).

    Returns AVAILABLE rooms for the property, optionally narrowed to a matching
    roomType, suitable for moving a guest into a ready room (PULSE VIP Room Not
    Ready, Requirement 4.2). "Ready" maps to the AVAILABLE status in the LUMI
    dataset (there is no distinct Ready status).

    Args:
        property_id: Property scope from the calling agent's session context.
        tool_input: Optional 'roomType' field to match candidate rooms.

    Returns:
        Success dict with the list of candidate rooms.
    """
    room_type = tool_input.get("roomType")

    rooms = _query_available_rooms(property_id, room_type)
    candidate_rooms = [
        {
            "roomNumber": room.get("roomNumber"),
            "roomType": room.get("roomType"),
            "status": room.get("status"),
        }
        for room in rooms
    ]

    return {
        "status": "success",
        "data": _decimal_to_native({
            "roomType": room_type,
            "candidateRooms": candidate_rooms,
        }),
    }


# ---------------------------------------------------------------------------
# Tool Registry and Router
# ---------------------------------------------------------------------------

TOOL_REGISTRY: Dict[str, Any] = {
    "get_occupancy": get_occupancy,
    "get_revenue": get_revenue,
    "get_vip_guests": get_vip_guests,
    "get_room_status": get_room_status,
    "get_work_orders": get_work_orders,
    # PULSE read-only tools (Decision 7: shared Gateway target).
    "get_sister_property_availability": get_sister_property_availability,
    "get_walkable_guests": get_walkable_guests,
    "get_room_move_candidates": get_room_move_candidates,
}


def _extract_tool_invocation(event: Dict[str, Any], context: Any) -> Tuple[str, Dict[str, Any]]:
    """Extract the tool name and tool input from a Lambda invocation.

    Per the AgentCore Gateway Lambda target contract, the Gateway invokes
    this function with `event` set to a flat map of the tool's input
    properties directly (not wrapped), and passes the invoked tool name via
    `context.client_context.custom["bedrockAgentCoreToolName"]` in the format
    `${target_name}___${tool_name}` (see AWS docs: "AWS Lambda function
    targets" - gateway-add-target-lambda.html). Falls back to a legacy
    `{"tool_name": ..., "tool_input": {...}}` event shape when no Gateway
    client context is present, so `aws lambda invoke` can still be used for
    direct manual testing (see tasks.md 13.2) without simulating a Gateway
    invocation.

    Args:
        event: The raw Lambda event - either the Gateway's flat tool-input
            map, or the legacy wrapped test payload.
        context: The Lambda context object, whose `client_context.custom`
            carries Gateway invocation metadata when present.

    Returns:
        A (tool_name, tool_input) tuple with the Gateway's target-name
        prefix already stripped from tool_name.
    """
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) if client_context else None

    if custom and "bedrockAgentCoreToolName" in custom:
        raw_tool_name = custom["bedrockAgentCoreToolName"]
        delimiter = "___"
        if delimiter in raw_tool_name:
            tool_name = raw_tool_name[raw_tool_name.index(delimiter) + len(delimiter) :]
        else:
            tool_name = raw_tool_name
        tool_input = event
    else:
        tool_name = event.get("tool_name", "")
        tool_input = event.get("tool_input", {})

    return tool_name, tool_input


def _route_tool(tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Route a tool invocation to the correct handler function.

    Extracts propertyId from tool_input, looks up the handler in the
    registry, and delegates execution. Returns structured error for
    unknown tools or missing propertyId.

    Args:
        tool_name: The tool name from the Gateway invocation event.
        tool_input: The tool parameters including propertyId.

    Returns:
        Success dict with tool data, or unavailability dict on error.
    """
    property_id = tool_input.get("propertyId", "")

    if not property_id:
        logger.warning("Missing propertyId in tool_input", extra={"tool_name": tool_name})
        return {
            "status": "unavailable",
            "message": f"Missing required parameter: propertyId",
        }

    handler = TOOL_REGISTRY.get(tool_name)
    if not handler:
        logger.warning(
            "Unknown tool requested",
            extra={"tool_name": tool_name, "property_id": property_id},
        )
        return {
            "status": "unavailable",
            "message": f"Unknown tool: {tool_name}",
        }

    return handler(property_id, tool_input)


# ---------------------------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------------------------


@tracer.capture_lambda_handler
@logger.inject_lambda_context
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Entry point for AgentCore Gateway tool invocations.

    Receives a tool invocation event from the Gateway, routes to the
    appropriate tool function, measures execution duration, and returns
    a structured response. Never raises unhandled exceptions - all error
    paths return a structured unavailability response.

    Args:
        event: Gateway invocation payload - a flat map of tool input
            properties (see `_extract_tool_invocation`), or the legacy
            `{"tool_name": ..., "tool_input": ...}` shape for direct testing.
        context: Lambda context object; `client_context.custom` carries the
            Gateway's invoked tool name when this is a real Gateway invocation.

    Returns:
        Dict with 'status' ("success" or "unavailable") and either 'data'
        or 'message' fields.
    """
    tool_name, tool_input = _extract_tool_invocation(event, context)
    property_id = tool_input.get("propertyId", "unknown")

    logger.info(
        "Tool invocation received",
        extra={
            "tool_name": tool_name,
            "property_id": property_id,
        },
    )

    start_time = time.time()

    try:
        result = _route_tool(tool_name, tool_input)
        status = result.get("status", "unknown")
    except ClientError as error:
        # Top-level safety net for any DynamoDB errors not caught by tool functions
        error_code = error.response["Error"]["Code"]
        logger.error(
            "DynamoDB error during tool execution",
            extra={
                "tool_name": tool_name,
                "property_id": property_id,
                "error_code": error_code,
                "error_message": str(error),
            },
        )
        result = {
            "status": "unavailable",
            "message": f"Data for {tool_name} is temporarily unavailable",
        }
        status = "unavailable"
    except Exception as error:
        # Catch any unexpected errors to prevent unhandled exceptions
        logger.error(
            "Unexpected error during tool execution",
            extra={
                "tool_name": tool_name,
                "property_id": property_id,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
        )
        result = {
            "status": "unavailable",
            "message": f"Data for {tool_name} is temporarily unavailable",
        }
        status = "unavailable"

    # Calculate duration and log structured context
    duration_ms = round((time.time() - start_time) * 1000, 2)

    logger.info(
        "Tool invocation completed",
        extra={
            "tool_name": tool_name,
            "property_id": property_id,
            "duration_ms": duration_ms,
            "status": status,
        },
    )

    # Add tracer metadata for X-Ray annotations
    tracer.put_metadata("tool_name", tool_name)
    tracer.put_metadata("property_id", property_id)
    tracer.put_metadata("response_status", status)

    return result
