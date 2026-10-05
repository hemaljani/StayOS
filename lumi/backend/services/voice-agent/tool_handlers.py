"""Voice Agent tool handlers - DynamoDB query layer for Nova Sonic tools.

Provides five async tool handler functions that query the hotel operations
dataset tables (reservations, rooms, guests, revenues, work-orders) scoped
by propertyId from the authenticated session context. Each handler wraps
synchronous boto3 DynamoDB calls with asyncio.to_thread() for non-blocking
I/O in the aiohttp event loop.

The dispatch_tool function routes Nova Sonic toolUse events to the correct
handler and catches errors at the boundary, returning structured unavailability
responses so Nova Sonic can communicate data gaps to the GM.

Role in project: Imported by nova_sonic_session.py when processing toolUse
events from the Nova Sonic bidirectional stream. Reuses the exact DynamoDB
access patterns established in data_puller.py.
"""

import asyncio
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

import boto3
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

logger = Logger(service="stayos-voice-agent")

# Dataset table names - populated by AgentCore Runtime environment variables.
# All table names derive from ${StackPrefix} at deploy time (Makefile
# voice-deploy target) rather than being hardcoded, so a second deployment
# with a different StackPrefix resolves to its own tables.
RESERVATIONS_TABLE_NAME = os.environ.get("RESERVATIONS_TABLE_NAME", "")
ROOMS_TABLE_NAME = os.environ.get("ROOMS_TABLE_NAME", "")
GUESTS_TABLE_NAME = os.environ.get("GUESTS_TABLE_NAME", "")
REVENUES_TABLE_NAME = os.environ.get("REVENUES_TABLE_NAME", "")
WORK_ORDERS_TABLE_NAME = os.environ.get("WORK_ORDERS_TABLE_NAME", "")
BRIEFS_TABLE_NAME = os.environ.get("BRIEFS_TABLE_NAME", "")

# VIP loyalty tiers that qualify for VIP arrival alerts
VIP_TIERS: Set[str] = {"AMBASSADOR", "TITANIUM", "PLATINUM"}

# Internal composite keys to strip from responses (not useful for Nova Sonic)
INTERNAL_KEYS: Set[str] = {"statusRoomNumber", "statusCreatedAt"}

# Module-level DynamoDB resource (connection reuse across all sessions)
_dynamodb_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)
_dynamodb_resource = boto3.resource("dynamodb", config=_dynamodb_config)


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
        # Convert to int if no fractional part, otherwise float
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
    implementation details of the GSI design, not meaningful to Nova Sonic.

    Args:
        item: A DynamoDB item dict.

    Returns:
        Item with internal composite keys removed.
    """
    return {k: v for k, v in item.items() if k not in INTERNAL_KEYS}


async def get_occupancy(
    property_id: str,
    date: Optional[str] = None,
) -> Dict[str, Any]:
    """Query occupancy metrics from the revenues table for a given date.

    Retrieves occupancy percentage, total arrivals, total departures,
    confirmed reservations, and available rooms from the daily revenue
    snapshot record.

    Args:
        property_id: Property scope from session context (partition key).
        date: ISO date string (YYYY-MM-DD). Defaults to today.

    Returns:
        Success dict with occupancy data, or unavailability dict on error.
    """
    date_str = date or _today_iso()
    logger.info(
        "Querying occupancy data",
        property_id=property_id,
        date=date_str,
        tool="getOccupancyTool",
    )

    # GetItem on stayos-revenues with composite key (propertyId, date)
    item = await asyncio.to_thread(
        _get_revenue_item, property_id, date_str
    )

    if not item:
        logger.info(
            "No revenue record found for date",
            property_id=property_id,
            date=date_str,
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


async def get_revenue(
    property_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Query revenue KPIs (ADR, RevPAR, comparisons) from the revenues table.

    Retrieves Average Daily Rate, Revenue Per Available Room, and
    period-over-period comparisons from the daily revenue snapshot.

    Parameter names use snake_case to match the get_revenue tool schema in
    tools_config.py (start_date, end_date). Nova Sonic sends the tool input
    keys verbatim and dispatch_tool forwards them as **params, so a mismatch
    here raises TypeError and the tool reports "temporarily unavailable".

    Args:
        property_id: Property scope from session context (partition key).
        start_date: ISO start date. Defaults to today.
        end_date: ISO end date. Defaults to start_date for single-day query.

    Returns:
        Success dict with revenue KPIs, or unavailability dict on error.
    """
    start = start_date or _today_iso()
    end = end_date or start
    logger.info(
        "Querying revenue data",
        property_id=property_id,
        start_date=start,
        end_date=end,
        tool="getRevenueTool",
    )

    # For single-day query, use GetItem; range queries not supported in seed data
    item = await asyncio.to_thread(
        _get_revenue_item, property_id, start
    )

    if not item:
        logger.info(
            "No revenue record found for date",
            property_id=property_id,
            date=start,
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


async def get_vip_guests(
    property_id: str,
    date: Optional[str] = None,
) -> Dict[str, Any]:
    """Query VIP guest arrivals from the daily brief.

    Reads the pre-generated brief's vipArrivals array to ensure the voice
    agent returns the same VIP list that the frontend UI displays. This
    avoids data inconsistencies between the brief (enriched by the
    orchestrator) and a raw reservations query.

    Args:
        property_id: Property scope from session context (partition key).
        date: ISO date for arrivals. Defaults to today.

    Returns:
        Success dict with VIP guest list from the brief.
    """
    date_str = date or _today_iso()
    logger.info(
        "Querying VIP guests from brief",
        property_id=property_id,
        date=date_str,
        tool="getVipGuestsTool",
    )

    # Read the brief's vipArrivals (same source the frontend uses)
    briefs_table = _dynamodb_resource.Table(BRIEFS_TABLE_NAME)
    response = await asyncio.to_thread(
        briefs_table.get_item,
        Key={"propertyId": property_id, "briefDate": date_str},
    )
    item = response.get("Item")

    if not item or not item.get("vipArrivals"):
        return {
            "status": "success",
            "data": {
                "date": date_str,
                "vipCount": 0,
                "guests": [],
                "message": f"No VIP arrivals found for {date_str}",
            },
        }

    vip_arrivals = item["vipArrivals"]

    # Strip sensitiveNotes if present (safety filter)
    for guest in vip_arrivals:
        guest.pop("sensitiveNotes", None)

    return {
        "status": "success",
        "data": _decimal_to_native({
            "date": date_str,
            "vipCount": len(vip_arrivals),
            "guests": vip_arrivals,
        }),
    }


async def get_room_status(property_id: str) -> Dict[str, Any]:
    """Query rooms currently out of order or in maintenance.

    Queries the rooms table GSI for OOO and MAINTENANCE status prefixes,
    returning room numbers, issue descriptions, and time in current state.

    Args:
        property_id: Property scope from session context (partition key).

    Returns:
        Success dict with OOO/maintenance room list.
    """
    logger.info(
        "Querying room status",
        property_id=property_id,
        tool="getRoomStatusTool",
    )

    rooms = await asyncio.to_thread(
        _query_ooo_maintenance_rooms, property_id
    )

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


async def get_work_orders(
    property_id: str,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Query open and in-progress work orders for the property.

    Queries the work-orders table GSI for OPEN and/or IN_PROGRESS status
    prefixes, returning work order details with priority and age.

    Args:
        property_id: Property scope from session context (partition key).
        status: Filter by status (OPEN or IN_PROGRESS). Defaults to both.

    Returns:
        Success dict with work order list.
    """
    logger.info(
        "Querying work orders",
        property_id=property_id,
        status_filter=status,
        tool="getWorkOrdersTool",
    )

    work_orders = await asyncio.to_thread(
        _query_work_orders_by_status, property_id, status
    )

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
# Tool Registry and Dispatcher
# ---------------------------------------------------------------------------

TOOL_REGISTRY: Dict[str, Any] = {
    "get_occupancy": get_occupancy,
    "get_revenue": get_revenue,
    "get_vip_guests": get_vip_guests,
    "get_room_status": get_room_status,
    "get_work_orders": get_work_orders,
}


async def dispatch_tool(
    tool_name: str,
    property_id: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Route a Nova Sonic toolUse event to the correct handler function.

    Acts as the top-level dispatcher with fallback error handling. Individual
    handlers catch specific exceptions where possible; this function catches
    any remaining ClientError as a safety net so a tool failure never crashes
    the session.

    Args:
        tool_name: The tool name from Nova Sonic's toolUse event.
        property_id: Property scope from the authenticated session context.
        params: Tool parameters from Nova Sonic (date, status, etc.).

    Returns:
        Success dict with tool data, or unavailability dict on error.
    """
    logger.info(
        "Dispatching tool invocation",
        tool_name=tool_name,
        property_id=property_id,
    )

    handler = TOOL_REGISTRY.get(tool_name)
    if not handler:
        logger.warning(
            "Unknown tool requested",
            tool_name=tool_name,
            property_id=property_id,
        )
        return {
            "status": "unavailable",
            "message": f"Unknown tool: {tool_name}",
        }

    try:
        return await handler(property_id, **params)
    except ClientError as error:
        error_code = error.response["Error"]["Code"]
        logger.error(
            "DynamoDB error during tool execution",
            tool_name=tool_name,
            property_id=property_id,
            error_code=error_code,
            error_message=str(error),
        )
        return {
            "status": "unavailable",
            "message": f"Data for {tool_name} is temporarily unavailable",
        }
    except Exception as error:
        # Catch unexpected errors (e.g., TypeError from hallucinated params)
        # to prevent a single tool failure from crashing the entire session.
        logger.error(
            "Unexpected error during tool execution",
            tool_name=tool_name,
            property_id=property_id,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return {
            "status": "unavailable",
            "message": f"Data for {tool_name} is temporarily unavailable",
        }


# ---------------------------------------------------------------------------
# Synchronous DynamoDB Query Functions (run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    """Return today's date as an ISO 8601 string (YYYY-MM-DD).

    Returns:
        Today's date string in ISO format.
    """
    return date.today().isoformat()


def _get_revenue_item(
    property_id: str,
    date_str: str,
) -> Optional[Dict[str, Any]]:
    """Retrieve a revenue record from stayos-revenues by composite key.

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


def _query_arrivals_by_date(
    property_id: str,
    date_str: str,
) -> List[Dict[str, Any]]:
    """Query today's arrivals from stayos-reservations using arrivalDate GSI.

    Uses the propertyId-arrivalDate-index GSI to efficiently retrieve
    all reservations arriving on the specified date.

    Args:
        property_id: The property identifier (GSI partition key).
        date_str: ISO date string for arrival date (GSI sort key).

    Returns:
        List of reservation items arriving on the specified date.
    """
    table = _dynamodb_resource.Table(RESERVATIONS_TABLE_NAME)
    response = table.query(
        IndexName="propertyId-arrivalDate-index",
        KeyConditionExpression=(
            Key("propertyId").eq(property_id) & Key("arrivalDate").eq(date_str)
        ),
    )
    items = response.get("Items", [])

    # Handle pagination if results exceed 1MB
    while response.get("LastEvaluatedKey"):
        response = table.query(
            IndexName="propertyId-arrivalDate-index",
            KeyConditionExpression=(
                Key("propertyId").eq(property_id) & Key("arrivalDate").eq(date_str)
            ),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    return items


def _enrich_vip_guests(
    property_id: str,
    reservations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Enrich VIP reservations with guest profiles, excluding sensitiveNotes.

    For each VIP reservation, fetches the guest profile from stayos-guests
    and assembles a response entry with name, tier, room, occasion, and
    preferences. The sensitiveNotes field is explicitly removed before
    returning (Requirement 5.5).

    Args:
        property_id: The property identifier for guest lookups.
        reservations: Deduplicated VIP reservation records.

    Returns:
        List of enriched VIP guest entries safe for Nova Sonic.
    """
    guests_table = _dynamodb_resource.Table(GUESTS_TABLE_NAME)
    enriched: List[Dict[str, Any]] = []

    for reservation in reservations:
        guest_id = str(reservation.get("guestId", ""))

        # Fetch guest profile using composite key (propertyId, guestId)
        guest_profile: Optional[Dict[str, Any]] = None
        if guest_id:
            response = guests_table.get_item(
                Key={"propertyId": property_id, "guestId": guest_id}
            )
            guest_profile = response.get("Item")

        profile = guest_profile or {}

        # Build enriched entry for Nova Sonic
        entry: Dict[str, Any] = {
            "guestName": reservation.get("guestName", "Unknown Guest"),
            "loyaltyTier": reservation.get("loyaltyTier", ""),
            "roomNumber": reservation.get("roomNumber", ""),
            "roomType": reservation.get("roomType", ""),
            "specialOccasion": profile.get("specialOccasion"),
            "preferences": profile.get("preferences", []),
            "totalStays": profile.get("totalStays", 0),
        }

        # CRITICAL: Filter out sensitiveNotes (Req 5.5) - never send to Nova Sonic
        entry.pop("sensitiveNotes", None)

        enriched.append(entry)

    return enriched


def _query_ooo_maintenance_rooms(property_id: str) -> List[Dict[str, Any]]:
    """Query rooms with OOO or MAINTENANCE status from stayos-rooms.

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
    """Query work orders from stayos-work-orders by status.

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
        # Default: query both OPEN and IN_PROGRESS
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
