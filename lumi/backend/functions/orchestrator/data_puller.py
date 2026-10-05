"""LUMI Data Puller - DynamoDB dataset integration layer.

Queries the 5 hotel operations dataset tables (reservations, rooms,
guests, revenues, work-orders) to assemble the raw property data
needed for daily brief generation.

When MOCK_MODE is enabled (legacy), returns hardcoded mock data.
Default behavior queries the DynamoDB dataset tables populated by
the seed-data Lambda at deploy time.
"""

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

from orchestrator_exceptions import AllSourcesFailedError

logger = Logger(service="stayos-orchestrator")

# Module-level configuration from environment variables
MOCK_MODE = os.environ.get("MOCK_MODE", "false").lower() == "true"

# Dataset table names - populated by CloudFormation environment variables
RESERVATIONS_TABLE = os.environ.get("RESERVATIONS_TABLE_NAME", "")
ROOMS_TABLE = os.environ.get("ROOMS_TABLE_NAME", "")
GUESTS_TABLE = os.environ.get("GUESTS_TABLE_NAME", "")
REVENUES_TABLE = os.environ.get("REVENUES_TABLE_NAME", "")
WORK_ORDERS_TABLE = os.environ.get("WORK_ORDERS_TABLE_NAME", "")

# Legacy external API config (retained for future real API integration)
SPOG_API_ENDPOINT = os.environ.get("SPOG_API_ENDPOINT", "")
MDP_API_ENDPOINT = os.environ.get("MDP_API_ENDPOINT", "")
SPOG_SECRET_ARN = os.environ.get("SPOG_SECRET_ARN", "")

# VIP loyalty tiers that qualify for VIP arrival alerts
VIP_TIERS = {"AMBASSADOR", "TITANIUM", "PLATINUM"}

# Room type display priority (premium rooms shown first in VIP list)
ROOM_TYPE_PRIORITY = {
    "PENTHOUSE": 0,
    "SUITE": 1,
    "KING_DELUXE": 2,
    "QUEEN_DELUXE": 3,
    "KING_STANDARD": 4,
}

# Maximum number of VIP arrivals to include in the brief (curated, not exhaustive)
MAX_VIP_ARRIVALS = 7

# Maximum individual VIP alert action items to avoid flooding the UI
MAX_VIP_ALERTS = 3

# Module-level DynamoDB resource (connection reuse across Lambda invocations)
_dynamodb_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)
_dynamodb_resource = boto3.resource("dynamodb", config=_dynamodb_config)


def pull_property_data(property_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Query dataset tables and assemble property data for brief generation.

    Main entry point for the data pull step. In MOCK_MODE, returns
    hardcoded data for backward compatibility. Otherwise, queries the
    5 DynamoDB dataset tables and assembles the combined result with
    graceful degradation per source.

    Args:
        property_id: The property identifier (e.g., "ALOHA-CHI-001").
        settings: GM settings dict including delivery preferences.

    Returns:
        Combined property data dict with keys: property, dailyKPIs,
        actionItems, vipArrivals, and dataSourceStatus.

    Raises:
        AllSourcesFailedError: When every dataset query fails.
    """
    if MOCK_MODE:
        logger.info("MOCK_MODE enabled - returning mock data", property_id=property_id)
        return _get_mock_data(property_id, settings)

    return _pull_dataset_data(property_id, settings)


def _pull_dataset_data(property_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Pull data from DynamoDB dataset tables with graceful degradation.

    Queries each of the 5 dataset tables independently. If a query fails,
    logs the error, sets that source to empty/None, and continues with
    partial data. If ALL queries fail, raises AllSourcesFailedError.

    Args:
        property_id: The property identifier.
        settings: GM settings dict.

    Returns:
        Combined property data assembled from dataset queries.

    Raises:
        AllSourcesFailedError: When all dataset queries fail.
    """
    today_str = date.today().isoformat()
    data_source_status: Dict[str, str] = {}
    failed_sources: List[str] = []

    # Query revenue data (GetItem on stayos-revenues)
    revenue: Optional[Dict[str, Any]] = None
    try:
        revenue = _query_revenue(property_id, today_str)
        data_source_status["REVENUE"] = "SUCCESS"
    except ClientError as error:
        logger.error(
            "Revenue query failed - continuing with partial data",
            property_id=property_id,
            error=str(error),
        )
        data_source_status["REVENUE"] = "FAILED"
        failed_sources.append("REVENUE")

    # Query today's arrivals (Query on reservations arrivalDate GSI)
    arrivals: List[Dict[str, Any]] = []
    try:
        arrivals = _query_arrivals(property_id, today_str)
        data_source_status["RESERVATIONS"] = "SUCCESS"
    except ClientError as error:
        logger.error(
            "Arrivals query failed - continuing with partial data",
            property_id=property_id,
            error=str(error),
        )
        data_source_status["RESERVATIONS"] = "FAILED"
        failed_sources.append("RESERVATIONS")

    # Query VIP arrivals (filter arrivals + enrich from guests table)
    vip_arrivals: List[Dict[str, Any]] = []
    try:
        vip_arrivals = _query_vip_arrivals(property_id, today_str, arrivals)
        data_source_status["GUESTS"] = "SUCCESS"
    except ClientError as error:
        logger.error(
            "VIP arrivals query failed - continuing with partial data",
            property_id=property_id,
            error=str(error),
        )
        data_source_status["GUESTS"] = "FAILED"
        failed_sources.append("GUESTS")

    # Query OOO/MAINTENANCE rooms (Query on rooms status GSI)
    ooo_rooms: List[Dict[str, Any]] = []
    try:
        ooo_rooms = _query_ooo_rooms(property_id)
        data_source_status["ROOMS"] = "SUCCESS"
    except ClientError as error:
        logger.error(
            "OOO rooms query failed - continuing with partial data",
            property_id=property_id,
            error=str(error),
        )
        data_source_status["ROOMS"] = "FAILED"
        failed_sources.append("ROOMS")

    # Query open work orders (Query on work-orders status GSI)
    open_work_orders: List[Dict[str, Any]] = []
    try:
        open_work_orders = _query_open_work_orders(property_id)
        data_source_status["WORK_ORDERS"] = "SUCCESS"
    except ClientError as error:
        logger.error(
            "Work orders query failed - continuing with partial data",
            property_id=property_id,
            error=str(error),
        )
        data_source_status["WORK_ORDERS"] = "FAILED"
        failed_sources.append("WORK_ORDERS")

    # Check if all sources failed
    total_sources = 5
    if len(failed_sources) >= total_sources:
        logger.error(
            "All dataset queries failed",
            property_id=property_id,
            failed_sources=failed_sources,
        )
        raise AllSourcesFailedError("All dataset table queries failed")

    if failed_sources:
        logger.warning(
            "Partial data available - some queries failed",
            property_id=property_id,
            failed_sources=failed_sources,
        )

    # Derive action items from actual queried data
    action_items = _derive_action_items(
        revenue=revenue,
        arrivals=arrivals,
        ooo_rooms=ooo_rooms,
        work_orders=open_work_orders,
        vip_arrivals=vip_arrivals,
        settings=settings,
    )

    return {
        "property": _get_property_metadata(property_id, settings),
        "dailyKPIs": _format_kpis(revenue, arrivals, vip_arrivals, today_str),
        "actionItems": action_items,
        "vipArrivals": vip_arrivals,
        "dataSourceStatus": data_source_status,
    }


def _query_revenue(property_id: str, date_str: str) -> Optional[Dict[str, Any]]:
    """Retrieve today's revenue/KPI record from stayos-revenues table.

    Uses GetItem with composite key (propertyId, date) for single-digit
    millisecond retrieval of the daily revenue snapshot.

    Args:
        property_id: The property identifier (partition key).
        date_str: ISO date string e.g. "2025-01-15" (sort key).

    Returns:
        Revenue item dict or None if no record exists for today.
    """
    table = _dynamodb_resource.Table(REVENUES_TABLE)
    response = table.get_item(Key={"propertyId": property_id, "date": date_str})
    return response.get("Item")


def _query_arrivals(property_id: str, date_str: str) -> List[Dict[str, Any]]:
    """Query today's arrivals from stayos-reservations using arrivalDate GSI.

    Uses the propertyId-arrivalDate-index GSI to efficiently retrieve
    all reservations arriving on the specified date.

    Args:
        property_id: The property identifier (GSI partition key).
        date_str: ISO date string for arrival date (GSI sort key).

    Returns:
        List of reservation items arriving today.
    """
    table = _dynamodb_resource.Table(RESERVATIONS_TABLE)
    response = table.query(
        IndexName="propertyId-arrivalDate-index",
        KeyConditionExpression=(
            Key("propertyId").eq(property_id) & Key("arrivalDate").eq(date_str)
        ),
    )
    items = response.get("Items", [])

    # Handle pagination if results exceed 1MB (unlikely for single-day arrivals)
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


def _query_vip_arrivals(
    property_id: str,
    date_str: str,
    arrivals: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Filter, enrich, rank, and curate today's unique VIP arrivals.

    Guest profiles are loaded before ranking because stay history and special
    occasions exist only in the guest table. Ranking combines actual room
    type, loyalty tier, notable status, and stay history, then caps the
    frontend list at ``MAX_VIP_ARRIVALS``.

    Args:
        property_id: The property identifier.
        date_str: ISO date string for arrival date.
        arrivals: Pre-queried list of today's reservations.

    Returns:
        Unique, enriched VIP entries ordered with premium room types first.

    Raises:
        ClientError: If a guest profile cannot be read from DynamoDB.
    """
    vip_reservations = [
        reservation
        for reservation in arrivals
        if reservation.get("loyaltyTier") in VIP_TIERS
    ]
    if not vip_reservations:
        return []

    # Deduplicate reservations before profile reads so a guest with multiple
    # rooms cannot consume multiple display slots or duplicate action cards.
    seen_guest_ids: Set[str] = set()
    unique_reservations: List[Dict[str, Any]] = []
    for reservation in vip_reservations:
        guest_id = str(reservation.get("guestId", ""))
        if guest_id and guest_id not in seen_guest_ids:
            seen_guest_ids.add(guest_id)
            unique_reservations.append(reservation)

    guests_table = _dynamodb_resource.Table(GUESTS_TABLE)
    enriched_candidates: List[Dict[str, Any]] = []
    for reservation in unique_reservations:
        guest_id = str(reservation.get("guestId", ""))
        guest_profile = _get_guest_profile(guests_table, property_id, guest_id)
        enriched_candidates.append(
            _build_vip_arrival_entry(reservation, guest_profile, date_str)
        )

    return _select_curated_vip_arrivals(enriched_candidates)


def _get_guest_profile(
    guests_table: Any, property_id: str, guest_id: str
) -> Optional[Dict[str, Any]]:
    """Retrieve a guest profile from stayos-guests by composite key.

    Args:
        guests_table: boto3 DynamoDB Table resource for stayos-guests.
        property_id: The property identifier (partition key).
        guest_id: The guest identifier (sort key).

    Returns:
        Guest profile dict or None if not found.
    """
    if not guest_id:
        return None

    response = guests_table.get_item(
        Key={"propertyId": property_id, "guestId": guest_id}
    )
    return response.get("Item")


def _build_vip_arrival_entry(
    reservation: Dict[str, Any],
    guest_profile: Optional[Dict[str, Any]],
    date_str: str,
) -> Dict[str, Any]:
    """Assemble a VIP arrival entry from reservation and guest profile.

    Combines reservation booking data with guest profile preferences
    to match the vipArrivals schema expected by the brief generator.

    Args:
        reservation: The reservation record from stayos-reservations.
        guest_profile: The guest profile from stayos-guests (may be None).
        date_str: ISO date string for the arrival date.

    Returns:
        VIP arrival dict matching the expected vipArrivals schema.
    """
    guest_name = reservation.get("guestName", "Unknown Guest")
    # Derive initials from guest name
    name_parts = guest_name.split()
    initials = "".join(part[0].upper() for part in name_parts if part)

    # Use guest profile data if available, fall back to reservation fields
    profile = guest_profile or {}

    # Derive varied arrival time from guestId hash to avoid all VIPs
    # showing the same default "14:00" arrival time
    guest_id = reservation.get("guestId", "")
    hour_offset = sum(ord(c) for c in guest_id) % 10
    arrival_hour = 13 + hour_offset  # Spread arrivals between 13:00 and 22:00
    arrival_minute = (sum(ord(c) for c in guest_id) * 7) % 60
    estimated_arrival = f"{date_str}T{arrival_hour:02d}:{arrival_minute:02d}:00"

    return {
        "guestId": reservation.get("guestId", ""),
        "guestName": guest_name,
        "initials": initials[:2],
        "loyaltyTier": reservation.get("loyaltyTier", ""),
        "loyaltyNumber": profile.get("loyaltyNumber", ""),
        "totalStays": _decimal_to_int(profile.get("totalStays", 0)),
        "roomNumber": reservation.get("roomNumber", ""),
        "roomType": reservation.get("roomType", ""),
        "estimatedArrival": estimated_arrival,
        "specialOccasion": profile.get("specialOccasion"),
        "preferences": profile.get("preferences", []),
        "accountType": profile.get("accountType", "PERSONAL"),
        "corporateAccount": profile.get("corporateAccount"),
    }


def _is_notable_vip(vip_arrival: Dict[str, Any]) -> bool:
    """Return whether a curated VIP warrants an individual alert card.

    Args:
        vip_arrival: Enriched VIP arrival entry.

    Returns:
        True for Ambassador or Titanium guests with a special occasion or
        more than 40 lifetime stays; otherwise False.
    """
    loyalty_tier = vip_arrival.get("loyaltyTier", "")
    total_stays = _decimal_to_int(vip_arrival.get("totalStays", 0))
    return loyalty_tier in {"AMBASSADOR", "TITANIUM"} and bool(
        vip_arrival.get("specialOccasion") or total_stays > 40
    )


def _vip_ranking_key(vip_arrival: Dict[str, Any]) -> Tuple[int, int, int, int, str]:
    """Build a deterministic ranking key for a VIP display candidate.

    Actual room type is the primary ordering dimension so scarce premium
    inventory is visible. Within each room type, notable guests, higher tiers,
    and longer stay histories rank first.

    Args:
        vip_arrival: Enriched VIP arrival entry.

    Returns:
        Sort key ordered from most to least operationally relevant.
    """
    tier_priority = {"AMBASSADOR": 0, "TITANIUM": 1, "PLATINUM": 2}
    return (
        ROOM_TYPE_PRIORITY.get(vip_arrival.get("roomType", ""), 5),
        0 if _is_notable_vip(vip_arrival) else 1,
        tier_priority.get(vip_arrival.get("loyaltyTier", ""), 3),
        -_decimal_to_int(vip_arrival.get("totalStays", 0)),
        str(vip_arrival.get("guestId", "")),
    )


def _select_curated_vip_arrivals(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Select a bounded VIP list with premium and notable representation.

    The ranked list naturally favors premium room types. If notable guests
    exist but the room-first cap excludes all of them, the strongest notable
    candidate replaces the last non-notable candidate so alert-worthy guests
    remain represented without fabricating room assignments.

    Args:
        candidates: Unique VIP entries already enriched with guest profiles.

    Returns:
        At most ``MAX_VIP_ARRIVALS`` ranked VIP entries.
    """
    ranked_candidates = sorted(candidates, key=_vip_ranking_key)
    selected = ranked_candidates[:MAX_VIP_ARRIVALS]
    notable_candidates = [candidate for candidate in ranked_candidates if _is_notable_vip(candidate)]

    if notable_candidates and not any(_is_notable_vip(candidate) for candidate in selected):
        selected[-1] = notable_candidates[0]
        selected.sort(key=_vip_ranking_key)

    return selected


def _query_ooo_rooms(property_id: str) -> List[Dict[str, Any]]:
    """Query rooms with OOO or MAINTENANCE status from stayos-rooms.

    Uses the propertyId-statusRoomNumber-index GSI with begins_with on
    the composite sort key to find rooms currently out of order or under
    maintenance.

    Args:
        property_id: The property identifier (GSI partition key).

    Returns:
        List of room dicts with OOO or MAINTENANCE status.
    """
    table = _dynamodb_resource.Table(ROOMS_TABLE)
    ooo_rooms: List[Dict[str, Any]] = []

    # Query for OOO rooms (statusRoomNumber begins with "OOO#")
    ooo_response = table.query(
        IndexName="propertyId-statusRoomNumber-index",
        KeyConditionExpression=(
            Key("propertyId").eq(property_id)
            & Key("statusRoomNumber").begins_with("OOO#")
        ),
    )
    ooo_rooms.extend(ooo_response.get("Items", []))

    # Query for MAINTENANCE rooms (statusRoomNumber begins with "MAINTENANCE#")
    maint_response = table.query(
        IndexName="propertyId-statusRoomNumber-index",
        KeyConditionExpression=(
            Key("propertyId").eq(property_id)
            & Key("statusRoomNumber").begins_with("MAINTENANCE#")
        ),
    )
    ooo_rooms.extend(maint_response.get("Items", []))

    return ooo_rooms


def _query_open_work_orders(property_id: str) -> List[Dict[str, Any]]:
    """Query open/in-progress work orders from stayos-work-orders.

    Uses the propertyId-statusCreatedAt-index GSI with begins_with on
    the composite sort key to find active work orders.

    Args:
        property_id: The property identifier (GSI partition key).

    Returns:
        List of work order dicts with OPEN or IN_PROGRESS status.
    """
    table = _dynamodb_resource.Table(WORK_ORDERS_TABLE)
    work_orders: List[Dict[str, Any]] = []

    # Query for OPEN work orders (statusCreatedAt begins with "OPEN#")
    open_response = table.query(
        IndexName="propertyId-statusCreatedAt-index",
        KeyConditionExpression=(
            Key("propertyId").eq(property_id)
            & Key("statusCreatedAt").begins_with("OPEN#")
        ),
    )
    work_orders.extend(open_response.get("Items", []))

    # Query for IN_PROGRESS work orders (statusCreatedAt begins with "IN_PROGRESS#")
    ip_response = table.query(
        IndexName="propertyId-statusCreatedAt-index",
        KeyConditionExpression=(
            Key("propertyId").eq(property_id)
            & Key("statusCreatedAt").begins_with("IN_PROGRESS#")
        ),
    )
    work_orders.extend(ip_response.get("Items", []))

    return work_orders


def _derive_action_items(
    revenue: Optional[Dict[str, Any]],
    arrivals: List[Dict[str, Any]],
    ooo_rooms: List[Dict[str, Any]],
    work_orders: List[Dict[str, Any]],
    vip_arrivals: List[Dict[str, Any]],
    settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Generate action items from actual queried data.

    Derives actionable alerts by analyzing the real dataset state rather
    than returning static templates. Each action type is conditionally
    generated based on data thresholds.

    Args:
        revenue: Today's revenue record (may be None if query failed).
        arrivals: List of today's reservation arrivals.
        ooo_rooms: List of rooms currently OOO or in maintenance.
        work_orders: List of open/in-progress work orders.
        vip_arrivals: List of VIP arrivals (already enriched).
        settings: GM settings for alert toggle preferences.

    Returns:
        List of action item dicts sorted by severity.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    action_items: List[Dict[str, Any]] = []
    alert_toggles = settings.get("alertToggles", {})

    # OVERBOOKING_RISK: triggered when confirmed > available
    if alert_toggles.get("overbookingRisk", True) and revenue:
        confirmed = _decimal_to_int(revenue.get("confirmedReservations", 0))
        available = _decimal_to_int(revenue.get("availableRooms", 0))
        if confirmed > available:
            overage = confirmed - available
            action_items.append({
                "id": f"action-overbooking-{date.today().isoformat()}",
                "type": "OVERBOOKING_RISK",
                "severity": "URGENT",
                "title": f"Overbooking Risk - +{overage} Rooms",
                "detail": (
                    f"{confirmed} confirmed vs {available} available. "
                    "Walk strategy required."
                ),
                "data": {
                    "confirmedCount": confirmed,
                    "availableRooms": available,
                    "overage": overage,
                },
                "generatedAt": now_iso,
                "source": "DATASET_REVENUE",
            })

    # ROOMS_OUT_OF_ORDER: triggered when OOO rooms exist
    if alert_toggles.get("roomsOutOfOrder", True) and ooo_rooms:
        # Rooms store only the active work-order ID; issue details live in the
        # work-orders table, so join the two source lists in memory.
        work_order_by_id = {
            str(work_order.get("workOrderId", "")): work_order
            for work_order in work_orders
            if work_order.get("workOrderId")
        }
        room_details = []
        now_local = datetime.now()
        for room in ooo_rooms:
            work_order_id = str(room.get("currentWorkOrderId", ""))
            matching_work_order = work_order_by_id.get(work_order_id, {})
            issue_type = matching_work_order.get("issueType") or room.get("status", "OOO")

            # Compute hours open from work order creation time.
            # Seed data stores naive timestamps in UTC (Lambda TZ).
            # If createdAt is in the future (seed distributes across 6-22h),
            # fall back to estimatedResolutionHours as a meaningful display value.
            open_hours = 0
            created_at_str = matching_work_order.get("createdAt", "")
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                    elapsed = (now_local - created_at).total_seconds()
                    if elapsed > 0:
                        open_hours = int(elapsed / 3600)
                    else:
                        # Work order timestamp is ahead of current time (seed artifact)
                        # Use estimated resolution hours as a proxy
                        est_hours = matching_work_order.get(
                            "estimatedResolutionHours", 0
                        )
                        open_hours = _decimal_to_int(est_hours) if est_hours else 1
                except (ValueError, TypeError):
                    open_hours = 0

            room_details.append({
                "roomNumber": room.get("roomNumber", ""),
                "issue": issue_type,
                "isPremium": bool(room.get("isPremiumRoom", False)),
                "view": room.get("view", "NONE"),
                "workOrderId": work_order_id,
                "openHours": open_hours,
            })

        action_items.append({
            "id": f"action-ooo-{date.today().isoformat()}",
            "type": "ROOMS_OUT_OF_ORDER",
            "severity": "URGENT",
            "title": f"{len(ooo_rooms)} Rooms Out of Order",
            "detail": _format_ooo_detail(room_details),
            "data": {
                "roomCount": len(ooo_rooms),
                "rooms": room_details,
            },
            "generatedAt": now_iso,
            "source": "DATASET_ROOMS",
        })

    # VIP_ARRIVAL_ALERT: create a bounded set of unique, notable alerts.
    if alert_toggles.get("vipArrivalAlert", True):
        alerted_guest_ids: Set[str] = set()
        for vip in vip_arrivals:
            guest_id = str(vip.get("guestId", ""))
            if (
                len(alerted_guest_ids) >= MAX_VIP_ALERTS
                or not guest_id
                or guest_id in alerted_guest_ids
                or not _is_notable_vip(vip)
            ):
                continue

            alerted_guest_ids.add(guest_id)
            total_stays = _decimal_to_int(vip.get("totalStays", 0))
            tier = str(vip.get("loyaltyTier", ""))
            special_occasion = vip.get("specialOccasion")
            guest_name = vip.get("guestName", "VIP Guest")
            room_number = vip.get("roomNumber", "")
            preferences = vip.get("preferences", [])

            detail_parts = [f"Room {room_number}"]
            if special_occasion:
                detail_parts.append(str(special_occasion).replace("_", " ").title())
            detail_parts.append(f"{total_stays} stays")
            if preferences:
                detail_parts.append(", ".join(preferences[:3]))

            action_items.append({
                "id": f"action-vip-{guest_id}",
                "type": "VIP_ARRIVAL_ALERT",
                "severity": "HIGH",
                "title": f"{tier.title()} VIP - {guest_name}",
                "detail": " - ".join(detail_parts),
                "data": {
                    "guestId": guest_id,
                    "guestName": guest_name,
                    "loyaltyTier": tier,
                    "totalStays": total_stays,
                    "roomNumber": room_number,
                    "roomType": vip.get("roomType", ""),
                    "specialOccasion": special_occasion,
                    "preferences": preferences,
                },
                "generatedAt": now_iso,
                "source": "DATASET_GUESTS",
            })

    # UPSELL_OPPORTUNITY: when revenue shows upsell-eligible arrivals
    if alert_toggles.get("upsellOpportunity", True) and revenue:
        upsell_eligible = _decimal_to_int(revenue.get("upsellEligible", 0))
        if upsell_eligible > 0:
            avg_upsell = _decimal_to_int(revenue.get("avgUpsellValue", 85))
            action_items.append({
                "id": f"action-upsell-{date.today().isoformat()}",
                "type": "UPSELL_OPPORTUNITY",
                "severity": "MEDIUM",
                "title": f"Upsell Opportunity - {upsell_eligible} Eligible Arrivals",
                "detail": (
                    f"{upsell_eligible} standard reservations eligible for "
                    f"suite upgrade. Avg upsell: +${avg_upsell}/night."
                ),
                "data": {
                    "eligibleCount": upsell_eligible,
                    "avgUpsellValuePerNight": avg_upsell,
                    "totalPotentialRevenue": upsell_eligible * avg_upsell,
                    "recommendation": "Brief front desk at morning standup",
                },
                "generatedAt": now_iso,
                "source": "DATASET_REVENUE",
            })

    # STAFFING_CONFIRMED: informational - always generated when arrivals exist
    if alert_toggles.get("staffingConfirmed", True) and revenue:
        group_departures = _decimal_to_int(
            revenue.get("departures", {}).get("groupRooms", 0)
            if isinstance(revenue.get("departures"), dict)
            else 0
        )
        if group_departures > 0:
            group_name = revenue.get("departures", {}).get("groupName", "Group") if isinstance(revenue.get("departures"), dict) else "Group"
            action_items.append({
                "id": f"action-staffing-{date.today().isoformat()}",
                "type": "STAFFING_CONFIRMED",
                "severity": "LOW",
                "title": "F&B Staffing Confirmed",
                "detail": (
                    f"Full team for {group_name} group checkout "
                    f"({group_departures} rooms)."
                ),
                "data": {
                    "groupName": group_name,
                    "groupRooms": group_departures,
                    "staffingStatus": "CONFIRMED",
                },
                "generatedAt": now_iso,
                "source": "DATASET_REVENUE",
            })

    return action_items


def _get_property_metadata(property_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Build property metadata from GM settings.

    Constructs the property section of the brief data from the settings
    record which contains property name, brand, and GM details.

    Args:
        property_id: The property identifier.
        settings: GM settings dict with property metadata fields.

    Returns:
        Property metadata dict matching the expected schema.
    """
    return {
        "propertyId": property_id,
        "propertyName": settings.get("propertyName", "Property"),
        "brand": settings.get("brand", "Aloha Hotels & Resorts"),
        "city": settings.get("city", ""),
        "state": settings.get("state", ""),
        "country": settings.get("country", ""),
        "timezone": settings.get("timezone", "UTC"),
        "totalRooms": _decimal_to_int(settings.get("totalRooms", 0)),
        "gmAlias": settings.get("gmAlias", ""),
        "gmName": settings.get("gmName", "General Manager"),
        "briefDeliveryTime": settings.get("briefDeliveryTime", "06:30"),
    }


def _count_curated_vip_tiers(
    vip_arrivals: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Count unique curated VIP guests by loyalty tier.

    Args:
        vip_arrivals: Curated VIP entries selected for frontend display.

    Returns:
        Counts whose tier totals always equal the unique VIP count.
    """
    tier_counts = {
        "AMBASSADOR": 0,
        "TITANIUM": 0,
        "PLATINUM": 0,
    }
    seen_guest_ids: Set[str] = set()
    for vip_arrival in vip_arrivals:
        guest_id = str(vip_arrival.get("guestId", ""))
        loyalty_tier = str(vip_arrival.get("loyaltyTier", ""))
        if guest_id and guest_id not in seen_guest_ids and loyalty_tier in tier_counts:
            seen_guest_ids.add(guest_id)
            tier_counts[loyalty_tier] += 1

    return {
        "vipCount": len(seen_guest_ids),
        "ambassadorCount": tier_counts["AMBASSADOR"],
        "titaniumCount": tier_counts["TITANIUM"],
        "platinumCount": tier_counts["PLATINUM"],
    }


def _derive_revpar_budget(
    current_revpar: int,
    occupancy_pct: int,
    occupancy_vs_budget: float,
) -> int:
    """Estimate budget RevPAR from the available occupancy budget delta.

    The prototype revenue dataset has no explicit RevPAR budget. Assuming ADR
    is held constant, budget RevPAR scales current RevPAR by budget occupancy,
    where budget occupancy is current occupancy minus ``vsBudget``.

    Args:
        current_revpar: Current RevPAR value.
        occupancy_pct: Current occupancy percentage.
        occupancy_vs_budget: Occupancy percentage-point delta versus budget.

    Returns:
        Defensible estimated budget RevPAR, or current RevPAR when occupancy
        is unavailable.
    """
    if occupancy_pct <= 0:
        return current_revpar
    budget_occupancy = max(0.0, occupancy_pct - occupancy_vs_budget)
    return round(current_revpar * budget_occupancy / occupancy_pct)


def _format_kpis(
    revenue: Optional[Dict[str, Any]],
    arrivals: List[Dict[str, Any]],
    vip_arrivals: List[Dict[str, Any]],
    date_str: str,
) -> Dict[str, Any]:
    """Format revenue and curated VIP data for the frontend KPI schema.

    The generator's shared ``vsLastWeek``, ``vsBudget``, and ``vsYOY`` fields
    are mapped directly to the comparison fields expected by the prototype UI.
    Because the generator has no intraday forecast, ``forecast3pm`` uses the
    current occupancy snapshot. ADR pace is derived as 100 plus the shared
    budget delta, and RevPAR budget is estimated at constant ADR.

    Args:
        revenue: Today's revenue record from stayos-revenues, if available.
        arrivals: Today's reservation rows, used only as a no-revenue fallback.
        vip_arrivals: Unique curated VIP entries selected for display.
        date_str: ISO date string for today.

    Returns:
        Formatted dailyKPIs dict matching the frontend schema.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    vip_counts = _count_curated_vip_tiers(vip_arrivals)

    if not revenue:
        return {
            "date": date_str,
            "asOf": now_iso,
            "occupancy": {"current": 0, "unit": "percent"},
            "adr": {"current": 0},
            "revPAR": {"current": 0},
            "arrivals": {"total": len(arrivals), **vip_counts},
            "departures": {"total": 0},
            "confirmedReservations": 0,
            "availableRooms": 0,
        }

    occupancy_pct = _decimal_to_int(revenue.get("occupancyPct", 0))
    current_revpar = _decimal_to_int(revenue.get("revpar", 0))
    vs_last_week = _decimal_to_float(revenue.get("vsLastWeek", 0))
    vs_budget = _decimal_to_float(revenue.get("vsBudget", 0))

    return {
        "date": date_str,
        "asOf": now_iso,
        "occupancy": {
            "current": occupancy_pct,
            "unit": "percent",
            "vsLastWeek": vs_last_week,
            "vsBudget": vs_budget,
            "forecast3pm": occupancy_pct,
        },
        "adr": {
            "current": _decimal_to_int(revenue.get("adr", 0)),
            "currency": revenue.get("currency", "USD"),
            "vsLastWeek": vs_last_week,
            "vsBudget": vs_budget,
            "pacePctOfBudget": max(0, round(100 + vs_budget)),
        },
        "revPAR": {
            "current": current_revpar,
            "currency": revenue.get("currency", "USD"),
            "vsYOY": _decimal_to_float(revenue.get("vsYOY", 0)),
            "budget": _derive_revpar_budget(current_revpar, occupancy_pct, vs_budget),
        },
        "arrivals": {
            "total": _decimal_to_int(revenue.get("arrivals", len(arrivals))),
            **vip_counts,
        },
        "departures": {
            "total": _decimal_to_int(revenue.get("departures", 0)),
            "groupCheckouts": 0,
            "groupRooms": 0,
        },
        "confirmedReservations": _decimal_to_int(revenue.get("confirmedReservations", 0)),
        "availableRooms": _decimal_to_int(revenue.get("availableRooms", 0)),
    }


def _format_ooo_detail(rooms: List[Dict[str, Any]]) -> str:
    """Format OOO room details into a human-readable summary string.

    Generates a concise text summary listing affected rooms and their
    issues for the action item detail field.

    Args:
        rooms: List of room detail dicts with roomNumber and issue fields.

    Returns:
        Formatted string summarizing OOO rooms.
    """
    if not rooms:
        return "No rooms currently out of order."

    details = []
    for room in rooms[:4]:
        room_num = room.get("roomNumber", "???")
        issue = room.get("issue", "Unknown")
        details.append(f"Rm {room_num} ({issue})")

    summary = ", ".join(details)
    if len(rooms) > 4:
        summary += f" +{len(rooms) - 4} more"

    return summary


def _decimal_to_int(value: Any) -> int:
    """Safely convert a DynamoDB Decimal or numeric value to int.

    DynamoDB returns numbers as Decimal objects. This helper converts
    them to native Python int for JSON serialization and arithmetic.

    Args:
        value: A Decimal, int, float, or other numeric value.

    Returns:
        Integer value, or 0 if conversion fails.
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _decimal_to_float(value: Any) -> float:
    """Safely convert a DynamoDB Decimal or numeric value to float.

    Used for delta values (vsLastWeek, vsBudget) that may have decimal
    places and need float representation.

    Args:
        value: A Decimal, int, float, or other numeric value.

    Returns:
        Float value, or 0.0 if conversion fails.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Legacy MOCK_MODE Data (backward compatibility)
# ---------------------------------------------------------------------------


def _get_mock_data(property_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Return hardcoded mock data matching the mock-data.json schema.

    Provides realistic test data for end-to-end pipeline testing
    without requiring access to the DynamoDB dataset tables. Retained
    for backward compatibility when MOCK_MODE=true.

    Args:
        property_id: The property identifier (used in response metadata).
        settings: GM settings dict (used for GM name and preferences).

    Returns:
        Complete property data dict matching mock-data.json structure.
    """
    gm_name = settings.get("gmName", "Jennifer Smith")
    gm_alias = settings.get("gmAlias", "jsmith")
    property_name = settings.get("propertyName", "Aloha Grand Chicago")
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    return {
        "property": {
            "propertyId": property_id,
            "propertyName": property_name,
            "brand": "Aloha Hotels & Resorts",
            "city": "Chicago",
            "state": "IL",
            "country": "US",
            "timezone": "America/Chicago",
            "totalRooms": 368,
            "gmAlias": gm_alias,
            "gmName": gm_name,
            "briefDeliveryTime": "06:30",
        },
        "dailyKPIs": {
            "date": today,
            "asOf": now_iso,
            "occupancy": {
                "current": 87,
                "unit": "percent",
                "vsLastWeek": 4.2,
                "vsBudget": 2.1,
                "forecast3pm": 91,
            },
            "adr": {
                "current": 248,
                "currency": "USD",
                "vsLastWeek": 12,
                "vsBudget": 8,
                "pacePctOfBudget": 103,
            },
            "revPAR": {
                "current": 216,
                "currency": "USD",
                "vsYOY": 7.1,
                "budget": 202,
            },
            "arrivals": {
                "total": 142,
                "vipCount": 7,
                "ambassadorCount": 3,
                "titaniumCount": 4,
                "platinumCount": 0,
            },
            "departures": {
                "total": 118,
                "groupCheckouts": 1,
                "groupRooms": 82,
            },
            "confirmedReservations": 374,
            "availableRooms": 368,
        },
        "actionItems": [
            {
                "id": "action-001",
                "type": "OVERBOOKING_RISK",
                "severity": "URGENT",
                "title": "Overbooking Risk - +6 Rooms",
                "detail": "374 confirmed vs 368 available. Walk strategy required by 7 AM.",
                "data": {
                    "confirmedCount": 374,
                    "availableRooms": 368,
                    "overage": 6,
                    "walkStrategy": {
                        "compProperty1": {
                            "name": "Aloha Midway",
                            "propertyId": "ALOHA-MDW-001",
                            "availableRooms": 3,
                        },
                        "compProperty2": {
                            "name": "Aloha O'Hare",
                            "propertyId": "ALOHA-ORD-001",
                            "availableRooms": 8,
                        },
                    },
                },
                "generatedAt": now_iso,
                "source": "SPOG_XPMS",
            },
            {
                "id": "action-002",
                "type": "ROOMS_OUT_OF_ORDER",
                "severity": "URGENT",
                "title": "4 Rooms Out of Order",
                "detail": "Rm 1204 & 1206 (HVAC), 0814 (plumbing), 2101 (deep clean). HotSOS WO #4421 open 28 hrs.",
                "data": {
                    "roomCount": 4,
                    "rooms": [
                        {
                            "roomNumber": "1204",
                            "issue": "HVAC failure",
                            "isPremium": True,
                            "view": "lake",
                            "workOrderId": "WO-4421",
                            "openHours": 28,
                        },
                        {
                            "roomNumber": "1206",
                            "issue": "HVAC failure",
                            "isPremium": True,
                            "view": "lake",
                            "workOrderId": "WO-4421",
                            "openHours": 28,
                        },
                        {
                            "roomNumber": "0814",
                            "issue": "Plumbing",
                            "isPremium": False,
                            "view": None,
                            "workOrderId": "WO-4398",
                            "openHours": 14,
                        },
                        {
                            "roomNumber": "2101",
                            "issue": "Deep clean",
                            "isPremium": False,
                            "view": None,
                            "workOrderId": "WO-4402",
                            "openHours": 6,
                        },
                    ],
                },
                "generatedAt": now_iso,
                "source": "SPOG_HOTSOS_GXP",
            },
            {
                "id": "action-003",
                "type": "VIP_ARRIVAL_ALERT",
                "severity": "HIGH",
                "title": "Ambassador VIP - David Chen",
                "detail": "Suite 2401 - Arrives 2:00 PM - 47 stays - Anniversary. Champagne on arrival, feather-free, high floor.",
                "data": {
                    "guestId": "ALH-MBR-00238471",
                    "guestName": "David Chen",
                    "loyaltyTier": "AMBASSADOR",
                    "loyaltyNumber": "AH-7821034",
                    "totalStays": 47,
                    "roomNumber": "2401",
                    "roomType": "SUITE",
                    "specialOccasion": "ANNIVERSARY",
                    "preferences": ["HIGH_FLOOR", "FEATHER_FREE_BEDDING", "CHAMPAGNE_ARRIVAL"],
                },
                "generatedAt": now_iso,
                "source": "SPOG_LOYALTY_CRM",
            },
            {
                "id": "action-004",
                "type": "UPSELL_OPPORTUNITY",
                "severity": "MEDIUM",
                "title": "Upsell Opportunity - 28 Eligible Arrivals",
                "detail": "28 standard reservations eligible for suite upgrade. Avg upsell: +$85/night.",
                "data": {
                    "eligibleCount": 28,
                    "avgUpsellValuePerNight": 85,
                    "totalPotentialRevenue": 2380,
                    "recommendation": "Brief front desk at 9 AM standup",
                },
                "generatedAt": now_iso,
                "source": "SPOG_XPMS_REVENUE",
            },
            {
                "id": "action-005",
                "type": "STAFFING_CONFIRMED",
                "severity": "LOW",
                "title": "F&B Staffing Confirmed",
                "detail": "Full team for Meridian Corp group checkout (82 rooms). Breakfast extended to 11 AM.",
                "data": {
                    "groupName": "Meridian Corp",
                    "groupRooms": 82,
                    "breakfastExtendedUntil": "11:00",
                    "staffingStatus": "CONFIRMED",
                },
                "generatedAt": now_iso,
                "source": "SPOG_GXP",
            },
        ],
        "vipArrivals": [
            {
                "guestId": "ALH-MBR-00238471",
                "guestName": "David Chen",
                "initials": "DC",
                "loyaltyTier": "AMBASSADOR",
                "loyaltyNumber": "AH-7821034",
                "totalStays": 47,
                "roomNumber": "2401",
                "roomType": "SUITE",
                "estimatedArrival": f"{today}T14:00:00-05:00",
                "specialOccasion": "ANNIVERSARY",
                "preferences": ["HIGH_FLOOR", "FEATHER_FREE_BEDDING", "CHAMPAGNE_ARRIVAL"],
                "accountType": "PERSONAL",
            },
            {
                "guestId": "ALH-MBR-00119823",
                "guestName": "Sarah Reeves",
                "initials": "SR",
                "loyaltyTier": "TITANIUM",
                "loyaltyNumber": "AH-4410298",
                "totalStays": 22,
                "roomNumber": "1802",
                "roomType": "KING_DELUXE",
                "estimatedArrival": f"{today}T16:30:00-05:00",
                "specialOccasion": None,
                "preferences": ["QUIET_FLOOR", "EXTRA_PILLOWS"],
                "accountType": "PERSONAL",
            },
            {
                "guestId": "ALH-MBR-00334562",
                "guestName": "Marcus Klein",
                "initials": "MK",
                "loyaltyTier": "PLATINUM",
                "loyaltyNumber": "AH-2295011",
                "totalStays": 11,
                "roomNumber": "1105",
                "roomType": "KING_STANDARD",
                "estimatedArrival": f"{today}T18:00:00-05:00",
                "specialOccasion": None,
                "preferences": ["HIGH_SPEED_WIFI", "EARLY_CHECKIN_REQUEST"],
                "accountType": "CORPORATE",
                "corporateAccount": "Meridian Corp",
            },
            {
                "guestId": "ALH-MBR-00278134",
                "guestName": "Lisa Park",
                "initials": "LP",
                "loyaltyTier": "TITANIUM",
                "loyaltyNumber": "AH-5567823",
                "totalStays": 18,
                "roomNumber": "2208",
                "roomType": "QUEEN_DELUXE",
                "estimatedArrival": f"{today}T19:30:00-05:00",
                "specialOccasion": None,
                "preferences": ["LATE_CHECKIN_NOTED"],
                "accountType": "PERSONAL",
            },
            {
                "guestId": "ALH-MBR-00091234",
                "guestName": "James Thornton",
                "initials": "JT",
                "loyaltyTier": "AMBASSADOR",
                "loyaltyNumber": "AH-1134567",
                "totalStays": 62,
                "roomNumber": "3001",
                "roomType": "PENTHOUSE",
                "estimatedArrival": f"{today}T20:00:00-05:00",
                "specialOccasion": None,
                "preferences": ["PENTHOUSE_LEVEL", "PRIVATE_CHECKIN", "NO_PUBLICITY"],
                "accountType": "PERSONAL",
            },
            {
                "guestId": "ALH-MBR-00445512",
                "guestName": "Priya Nair",
                "initials": "PN",
                "loyaltyTier": "AMBASSADOR",
                "loyaltyNumber": "AH-8830145",
                "totalStays": 31,
                "roomNumber": "2504",
                "roomType": "SUITE",
                "estimatedArrival": f"{today}T21:00:00-05:00",
                "specialOccasion": "BIRTHDAY",
                "preferences": ["VEGETARIAN_AMENITY", "HIGH_FLOOR"],
                "accountType": "CORPORATE",
                "corporateAccount": "Apex Financial Group",
            },
            {
                "guestId": "ALH-MBR-00556789",
                "guestName": "Robert Yuen",
                "initials": "RY",
                "loyaltyTier": "TITANIUM",
                "loyaltyNumber": "AH-6620341",
                "totalStays": 15,
                "roomNumber": "1610",
                "roomType": "KING_STANDARD",
                "estimatedArrival": f"{today}T22:30:00-05:00",
                "specialOccasion": None,
                "preferences": ["HYPOALLERGENIC_ROOM"],
                "accountType": "PERSONAL",
            },
        ],
        "dataSourceStatus": {
            "SPOG_XPMS": "MOCK",
            "SPOG_REVENUE": "MOCK",
            "SPOG_LOYALTY_CRM": "MOCK",
            "SPOG_HOTSOS_GXP": "MOCK",
            "MDP": "MOCK",
        },
    }


# ---------------------------------------------------------------------------
# Legacy External API Functions (retained for future real API integration)
# ---------------------------------------------------------------------------

# Module-level boto3 client for Secrets Manager (connection reuse)
_secrets_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)
_secrets_client = boto3.client("secretsmanager", config=_secrets_config)

# Cached API keys to avoid repeated Secrets Manager calls per invocation
_cached_spog_api_key: str = ""
_cached_mdp_api_key: str = ""


def _pull_live_data(property_id: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Pull data from real SPOG/MDP APIs with graceful degradation.

    Retained for future integration when real hotel APIs become
    available. Currently all sources raise DataPullError.

    Args:
        property_id: The property identifier.
        settings: GM settings dict.

    Returns:
        Combined property data with dataSourceStatus flags.

    Raises:
        AllSourcesFailedError: When all sources are unavailable.
    """
    from orchestrator_exceptions import DataPullError

    data_source_status: Dict[str, str] = {}
    combined_data: Dict[str, Any] = {
        "property": {},
        "dailyKPIs": {},
        "actionItems": [],
        "vipArrivals": [],
        "dataSourceStatus": data_source_status,
    }

    sources = [
        ("SPOG_XPMS", _pull_xpms_data),
        ("SPOG_REVENUE", _pull_revenue_data),
        ("SPOG_LOYALTY_CRM", _pull_loyalty_data),
        ("SPOG_HOTSOS_GXP", _pull_hotsos_data),
        ("MDP", _pull_mdp_data),
    ]

    failed_sources: List[str] = []

    for source_name, puller_fn in sources:
        try:
            source_data = puller_fn(property_id)
            data_source_status[source_name] = "SUCCESS"
            _merge_source_data(combined_data, source_name, source_data)
        except DataPullError as error:
            logger.warning(
                "Data source failed - continuing with available data",
                source=source_name,
                error=str(error),
                property_id=property_id,
            )
            data_source_status[source_name] = "FAILED"
            failed_sources.append(source_name)

    if len(failed_sources) == len(sources):
        raise AllSourcesFailedError()

    return combined_data


def _merge_source_data(
    combined: Dict[str, Any], source_name: str, source_data: Dict[str, Any]
) -> None:
    """Merge data from a single source into the combined result.

    Updates the combined dict in-place with data from the source.

    Args:
        combined: The combined data dict being assembled.
        source_name: Name of the source for routing logic.
        source_data: Data returned by the individual source puller.
    """
    if source_name == "SPOG_XPMS":
        combined["property"].update(source_data.get("property", {}))
        combined["dailyKPIs"].update(source_data.get("dailyKPIs", {}))
        combined["actionItems"].extend(source_data.get("actionItems", []))
    elif source_name == "SPOG_REVENUE":
        combined["dailyKPIs"].update(source_data.get("dailyKPIs", {}))
        combined["actionItems"].extend(source_data.get("actionItems", []))
    elif source_name == "SPOG_LOYALTY_CRM":
        combined["vipArrivals"].extend(source_data.get("vipArrivals", []))
        combined["actionItems"].extend(source_data.get("actionItems", []))
    elif source_name == "SPOG_HOTSOS_GXP":
        combined["actionItems"].extend(source_data.get("actionItems", []))
    elif source_name == "MDP":
        combined["dailyKPIs"].update(source_data.get("dailyKPIs", {}))


def _pull_xpms_data(property_id: str) -> Dict[str, Any]:
    """Pull reservation, OOO room, and overbooking data from SPOG xPMS.

    Args:
        property_id: The property identifier.

    Returns:
        Dict with property metadata, occupancy KPIs, and action items.

    Raises:
        DataPullError: If the xPMS API call fails.
    """
    from orchestrator_exceptions import DataPullError
    raise DataPullError("SPOG_XPMS", "Real SPOG xPMS API not configured for prototype")


def _pull_revenue_data(property_id: str) -> Dict[str, Any]:
    """Pull ADR, RevPAR, and occupancy data from SPOG Revenue Management.

    Args:
        property_id: The property identifier.

    Returns:
        Dict with revenue KPIs and upsell action items.

    Raises:
        DataPullError: If the Revenue Management API call fails.
    """
    from orchestrator_exceptions import DataPullError
    raise DataPullError("SPOG_REVENUE", "Real SPOG Revenue API not configured for prototype")


def _pull_loyalty_data(property_id: str) -> Dict[str, Any]:
    """Pull VIP profiles and preferences from SPOG Loyalty/CRM.

    Args:
        property_id: The property identifier.

    Returns:
        Dict with VIP arrivals and VIP-related action items.

    Raises:
        DataPullError: If the Loyalty/CRM API call fails.
    """
    from orchestrator_exceptions import DataPullError
    raise DataPullError("SPOG_LOYALTY_CRM", "Real SPOG Loyalty API not configured for prototype")


def _pull_hotsos_data(property_id: str) -> Dict[str, Any]:
    """Pull work orders and facilities data from SPOG HotSOS/GXP.

    Args:
        property_id: The property identifier.

    Returns:
        Dict with facilities-related action items.

    Raises:
        DataPullError: If the HotSOS/GXP API call fails.
    """
    from orchestrator_exceptions import DataPullError
    raise DataPullError("SPOG_HOTSOS_GXP", "Real SPOG HotSOS API not configured for prototype")


def _pull_mdp_data(property_id: str) -> Dict[str, Any]:
    """Pull reservation and rate data from the Master Data Platform.

    Args:
        property_id: The property identifier.

    Returns:
        Dict with supplementary KPI data from MDP.

    Raises:
        DataPullError: If the MDP API call fails.
    """
    from orchestrator_exceptions import DataPullError
    raise DataPullError("MDP", "Real MDP API not configured for prototype")
