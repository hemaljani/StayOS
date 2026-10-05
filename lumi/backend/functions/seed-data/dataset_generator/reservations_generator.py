"""Reservation generator for the LUMI hotel dataset seeder.

Generates ~45,000 reservation items across 5 pilot properties over a 30-day
window. Each reservation references a valid room from stayos-rooms and a valid
guest from stayos-guests, with arrival counts matching the revenue occupancy
targets from stayos-revenues.

Reservation patterns:
    - Arrival volume per property-day matches revenue_lookup arrivals field
    - Stay lengths: 1-5 nights weighted by property type (business vs leisure)
    - Channels: DIRECT 30%, OTA 25%, CORPORATE 20%, GROUP 15%, LOYALTY 10%
    - Status: CONFIRMED (future), CHECKED_IN (today arrivals), CHECKED_OUT (past),
      3% NO_SHOW, 3% CANCELLED
    - Group events: 2-3 per property (40-120 rooms, 2-4 nights)

All generation is deterministic (no randomness). Variance comes from reservation
index modulo patterns, property-specific stay weights, and event calendar config.

Supports REQ-DS-5 (30-day reservation volume), REQ-DS-8 (deterministic generation),
REQ-DS-9 (cross-table consistency), and REQ-DS-10 (agent queryability via GSI).
"""

import logging
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from dataset_generator.config import (
    CHANNEL_CUMULATIVE_THRESHOLDS,
    CHANNEL_TO_RATE_CODE,
    CORPORATE_ACCOUNTS,
    PROPERTY_PROFILES,
    PROPERTY_STAY_WEIGHTS,
    ROOM_TYPE_RATE_MULTIPLIERS,
    SEED_DAYS,
    TTL_RESERVATIONS_DAYS,
)
from dataset_generator.reference_date import resolve_reference_date
from dataset_generator.writer import BatchWriter

logger = logging.getLogger(__name__)

# Property short codes for reservationId generation (R-CHI-00001, R-MIA-01234, etc.)
PROPERTY_SHORT_CODES: Dict[str, str] = {
    "ALOHA-CHI-001": "CHI",
    "ALOHA-MIA-001": "MIA",
    "ALOHA-TYO-001": "TYO",
    "ALOHA-MAD-001": "MAD",
    "ALOHA-BOM-001": "BOM",
}


def _generate_reservation_id(property_id: str, sequence: int) -> str:
    """Generate a unique reservation ID from property short code and sequence.

    Pattern: R-{SHORT_CODE}-{5-digit sequence} (e.g., "R-CHI-00001").
    Sequence is 1-based.

    Args:
        property_id: The property identifier (e.g., "ALOHA-CHI-001").
        sequence: One-based sequence number for this reservation.

    Returns:
        Formatted reservation ID string.
    """
    short_code = PROPERTY_SHORT_CODES[property_id]
    return f"R-{short_code}-{sequence:05d}"


def _determine_stay_length(property_id: str, reservation_index: int) -> int:
    """Select stay length (1-5 nights) using weighted distribution.

    Uses PROPERTY_STAY_WEIGHTS cumulative sum to deterministically pick
    a stay length based on reservation_index. The index modulo total weight
    maps into the cumulative ranges.

    Args:
        property_id: The property identifier for weight lookup.
        reservation_index: Zero-based reservation index used as seed.

    Returns:
        Integer stay length in nights (1-5).
    """
    weights = PROPERTY_STAY_WEIGHTS[property_id]
    total_weight = sum(weights)

    # Deterministic position within the weight space
    position = reservation_index % total_weight

    # Walk cumulative weights to find the stay length
    cumulative = 0
    for nights_minus_one, weight in enumerate(weights):
        cumulative += weight
        if position < cumulative:
            return nights_minus_one + 1

    # Fallback: return max stay length
    return len(weights)


def _determine_channel(reservation_index: int) -> str:
    """Assign booking channel based on reservation index and cumulative thresholds.

    Uses CHANNEL_CUMULATIVE_THRESHOLDS from config. The index modulo 100
    maps into the threshold ranges:
        0-29 = DIRECT, 30-54 = OTA, 55-74 = CORPORATE, 75-89 = GROUP, 90-99 = LOYALTY

    Args:
        reservation_index: Zero-based reservation index used as seed.

    Returns:
        Channel string (DIRECT, OTA, CORPORATE, GROUP, or LOYALTY).
    """
    position = reservation_index % 100

    for channel, threshold in CHANNEL_CUMULATIVE_THRESHOLDS:
        if position < threshold:
            return channel

    # Fallback: last channel
    return CHANNEL_CUMULATIVE_THRESHOLDS[-1][0]


def _determine_status(
    arrival_date: date,
    departure_date: date,
    today: date,
    reservation_index: int,
) -> str:
    """Determine reservation status based on date relationship and index.

    Status logic:
        - arrivalDate > today -> CONFIRMED (with 3% CANCELLED override)
        - arrivalDate == today -> CHECKED_IN
        - departureDate <= today -> CHECKED_OUT (with 3% NO_SHOW override)
        - arrivalDate < today and departureDate > today -> CHECKED_IN (still in hotel)

    NO_SHOW: reservation_index % 33 == 0 for past reservations (3%)
    CANCELLED: reservation_index % 33 == 1 for future reservations (3%)

    Args:
        arrival_date: The reservation's arrival date.
        departure_date: The reservation's departure date.
        today: The current date reference.
        reservation_index: Zero-based index for NO_SHOW/CANCELLED determination.

    Returns:
        Status string: CONFIRMED, CHECKED_IN, CHECKED_OUT, NO_SHOW, or CANCELLED.
    """
    if arrival_date > today:
        # Future reservation - 3% cancelled
        if reservation_index % 33 == 1:
            return "CANCELLED"
        return "CONFIRMED"

    if arrival_date == today:
        # Today's arrival - checked in
        return "CHECKED_IN"

    # Past arrival
    if departure_date <= today:
        # Already departed - 3% no-show
        if reservation_index % 33 == 0:
            return "NO_SHOW"
        return "CHECKED_OUT"

    # Arrived in the past but not yet departed - still in hotel
    return "CHECKED_IN"


def _compute_nightly_rate(
    property_adr: Decimal,
    room_type: str,
) -> Decimal:
    """Compute nightly rate as property ADR multiplied by room type factor.

    Args:
        property_adr: The ADR from the revenue record for this property-day.
        room_type: Room type string for multiplier lookup.

    Returns:
        Decimal nightly rate rounded to 2 decimal places.
    """
    multiplier = ROOM_TYPE_RATE_MULTIPLIERS.get(room_type, Decimal("1.00"))
    rate = property_adr * multiplier
    return rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _compute_ttl(arrival_date: date) -> int:
    """Calculate DynamoDB TTL value for a reservation record.

    TTL is set to the arrival date plus TTL_RESERVATIONS_DAYS (60 days),
    converted to Unix epoch seconds.

    Args:
        arrival_date: The reservation's arrival date.

    Returns:
        Integer Unix epoch timestamp for the TTL expiration.
    """
    expiry_date = arrival_date + timedelta(days=TTL_RESERVATIONS_DAYS)
    epoch = int(time.mktime(expiry_date.timetuple()))
    return epoch


def _assign_guest(
    guests: List[Dict[str, Any]],
    reservation_index: int,
    room_is_premium: bool,
) -> Dict[str, Any]:
    """Assign a guest to a reservation using modulo rotation.

    VIP guests (Ambassador/Titanium, indices 0-34) are preferentially
    assigned to premium rooms. For non-premium rooms, rotation starts
    from index 15 (Titanium/Platinum range) to spread assignments.

    Args:
        guests: List of 50 guest dicts for this property.
        reservation_index: Zero-based index for modulo rotation.
        room_is_premium: Whether the assigned room is premium.

    Returns:
        The selected guest dict from the guests list.
    """
    guest_count = len(guests)

    if room_is_premium:
        # Premium rooms get top-tier guests (Ambassador/Titanium: indices 0-34)
        # Cap at actual guest list size for safety
        premium_pool_size = min(35, guest_count)
        guest_index = reservation_index % premium_pool_size
    else:
        # Non-premium rooms rotate through all guests with offset
        guest_index = (reservation_index + 15) % guest_count

    return guests[guest_index]


def _assign_special_requests(guest: Dict[str, Any], reservation_index: int) -> List[str]:
    """Select a subset of the guest's preferences as special requests.

    Takes 1-3 preferences from the guest's full preference list based on
    the reservation index, simulating that guests don't always request
    all their preferences for every stay.

    Args:
        guest: Guest dict containing a "preferences" list.
        reservation_index: Index used to determine subset size and offset.

    Returns:
        List of 1-3 preference strings (or empty if guest has none).
    """
    preferences = guest.get("preferences", [])
    if not preferences:
        return []

    # Take 1 to min(3, len(preferences)) items
    num_requests = (reservation_index % 3) + 1
    num_requests = min(num_requests, len(preferences))

    # Start offset rotates through preferences list
    start = reservation_index % len(preferences)
    requests: List[str] = []
    for i in range(num_requests):
        idx = (start + i) % len(preferences)
        requests.append(preferences[idx])

    return requests


def _assign_group_name(channel: str, reservation_index: int) -> Optional[str]:
    """Assign a group name for GROUP channel reservations.

    Uses CORPORATE_ACCOUNTS pool for group names via modulo rotation.
    Only GROUP channel reservations receive a group name.

    Args:
        channel: The reservation's booking channel.
        reservation_index: Index for group name rotation.

    Returns:
        Group name string for GROUP channel, None otherwise.
    """
    if channel != "GROUP":
        return None
    return CORPORATE_ACCOUNTS[reservation_index % len(CORPORATE_ACCOUNTS)]


def _generate_group_reservations(
    property_id: str,
    rooms: List[Dict[str, Any]],
    guests: List[Dict[str, Any]],
    revenue_lookup: Dict[Tuple[str, str], Dict[str, Any]],
    start_date: date,
    today: date,
    sequence_start: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """Generate group event reservations from property event calendar.

    Each property has 2-3 events in its eventCalendar (config.py). Each event
    produces a block of GROUP reservations for the specified room count over
    the specified number of nights, starting in the event's ISO week number.

    Args:
        property_id: The property identifier.
        rooms: List of room dicts for this property.
        guests: List of guest dicts for this property.
        revenue_lookup: Dict mapping (propertyId, date_str) to revenue items.
        start_date: First date of the 30-day seed window.
        today: Current date reference for status determination.
        sequence_start: Starting sequence number for reservation ID generation.

    Returns:
        Tuple of (list of group reservation dicts, next sequence number).
    """
    # Find property profile for event calendar
    profile: Optional[Dict[str, Any]] = None
    for p in PROPERTY_PROFILES:
        if p["propertyId"] == property_id:
            profile = p
            break

    if profile is None:
        logger.warning("No profile found for %s - skipping group events", property_id)
        return [], sequence_start

    event_calendar = profile.get("eventCalendar", [])
    if not event_calendar:
        return [], sequence_start

    group_reservations: List[Dict[str, Any]] = []
    current_sequence = sequence_start

    for event_index, event in enumerate(event_calendar):
        event_name: str = event["name"]
        event_rooms: int = event["rooms"]
        event_nights: int = event["nights"]
        event_week: int = event["weekNumber"]

        # Calculate the event start date within our 30-day window.
        # weekNumber 1 = first week (days 0-6), weekNumber 2 = second week, etc.
        event_start_day_index = (event_week - 1) * 7
        event_start_date = start_date + timedelta(days=event_start_day_index)

        # Ensure event falls within our seed window
        event_end_date = event_start_date + timedelta(days=event_nights)
        window_end = start_date + timedelta(days=SEED_DAYS - 1)
        if event_start_date > window_end:
            continue

        logger.info(
            "Generating group event '%s' for %s: %d rooms, %d nights starting %s",
            event_name,
            property_id,
            event_rooms,
            event_nights,
            event_start_date.isoformat(),
        )

        # Generate one reservation per room in the event block
        for room_idx in range(event_rooms):
            # Assign room by cycling through property rooms
            room = rooms[(current_sequence + room_idx) % len(rooms)]
            room_number: str = room["roomNumber"]
            room_type: str = room["roomType"]
            is_premium: bool = room.get("isPremiumRoom", False)

            # Assign guest via rotation
            guest = _assign_guest(guests, current_sequence + room_idx, is_premium)
            guest_id: str = guest["guestId"]
            guest_name: str = guest["name"]
            loyalty_tier: str = guest["loyaltyTier"]

            # All group reservations share the event dates
            arrival_date = event_start_date
            departure_date = event_end_date
            stay_nights = event_nights

            # Status based on date
            status = _determine_status(
                arrival_date, departure_date, today, current_sequence + room_idx
            )

            # Rate: use revenue ADR for event start date
            date_str = arrival_date.isoformat()
            revenue_item = revenue_lookup.get((property_id, date_str))
            if revenue_item:
                property_adr = revenue_item["adr"]
            else:
                # Fallback to profile baseline if date not in lookup
                property_adr = profile["adrBaseline"]

            nightly_rate = _compute_nightly_rate(property_adr, room_type)
            total_amount = (nightly_rate * Decimal(str(stay_nights))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            # Group reservations use GROUP channel and group rate code
            channel = "GROUP"
            rate_code = CHANNEL_TO_RATE_CODE[channel]

            # Special requests from guest preferences
            special_requests = _assign_special_requests(
                guest, current_sequence + room_idx
            )

            # TTL based on arrival date
            ttl = _compute_ttl(arrival_date)

            # Build reservation ID
            reservation_id = _generate_reservation_id(
                property_id, current_sequence + room_idx + 1
            )

            # Composite sort key: arrivalDate#reservationId
            date_reservation_id = f"{date_str}#{reservation_id}"

            reservation_item: Dict[str, Any] = {
                "propertyId": property_id,
                "dateReservationId": date_reservation_id,
                "arrivalDate": date_str,
                "departureDate": departure_date.isoformat(),
                "reservationId": reservation_id,
                "guestId": guest_id,
                "guestName": guest_name,
                "loyaltyTier": loyalty_tier,
                "roomNumber": room_number,
                "roomType": room_type,
                "status": status,
                "channel": channel,
                "rateCode": rate_code,
                "nightlyRate": nightly_rate,
                "totalAmount": total_amount,
                "stayNights": stay_nights,
                "groupName": event_name,
                "specialRequests": special_requests,
                "ttl": ttl,
            }

            group_reservations.append(reservation_item)

        current_sequence += event_rooms

    logger.info(
        "Generated %d group event reservations for %s",
        len(group_reservations),
        property_id,
    )

    return group_reservations, current_sequence


def _generate_daily_reservations(
    property_id: str,
    rooms: List[Dict[str, Any]],
    guests: List[Dict[str, Any]],
    revenue_lookup: Dict[Tuple[str, str], Dict[str, Any]],
    start_date: date,
    today: date,
    profile: Dict[str, Any],
    sequence_start: int,
    total_days: int = SEED_DAYS,
) -> Tuple[List[Dict[str, Any]], int]:
    """Generate daily (non-group) reservations matching revenue arrival targets.

    For each day in the seed window (including extra future days), reads the
    target arrival count from revenue_lookup and generates that many new
    reservations. Each reservation is assigned a room, guest, channel, stay
    length, and status deterministically.

    Args:
        property_id: The property identifier.
        rooms: List of room dicts for this property.
        guests: List of guest dicts for this property.
        revenue_lookup: Dict mapping (propertyId, date_str) to revenue items.
        start_date: First date of the 30-day seed window.
        today: Current date reference for status determination.
        profile: Property profile dict (for ADR baseline fallback).
        sequence_start: Starting sequence number for reservation ID generation.
        total_days: Total number of days to generate (SEED_DAYS + extra future days).

    Returns:
        Tuple of (list of daily reservation dicts, next sequence number).
    """
    daily_reservations: List[Dict[str, Any]] = []
    current_sequence = sequence_start

    for day_index in range(total_days):
        record_date = start_date + timedelta(days=day_index)
        date_str = record_date.isoformat()

        # Get target arrivals from revenue data
        revenue_item = revenue_lookup.get((property_id, date_str))
        if revenue_item:
            target_arrivals = revenue_item["arrivals"]
            property_adr = revenue_item["adr"]
        else:
            # Fallback: estimate arrivals from total rooms and average occupancy
            target_arrivals = profile["totalRooms"] // 5
            property_adr = profile["adrBaseline"]

        # Generate one reservation per target arrival
        for arrival_index in range(target_arrivals):
            reservation_index = current_sequence + arrival_index

            # Assign room by cycling through rooms list
            room = rooms[reservation_index % len(rooms)]
            room_number: str = room["roomNumber"]
            room_type: str = room["roomType"]
            is_premium: bool = room.get("isPremiumRoom", False)

            # Assign guest via rotation (premium bias for premium rooms)
            guest = _assign_guest(guests, reservation_index, is_premium)
            guest_id: str = guest["guestId"]
            guest_name: str = guest["name"]
            loyalty_tier: str = guest["loyaltyTier"]

            # Determine stay length from weighted distribution
            stay_nights = _determine_stay_length(property_id, reservation_index)

            # Calculate departure date
            arrival_date = record_date
            departure_date = arrival_date + timedelta(days=stay_nights)

            # Determine channel from cumulative thresholds
            channel = _determine_channel(reservation_index)
            rate_code = CHANNEL_TO_RATE_CODE[channel]

            # Determine status based on dates and index
            status = _determine_status(
                arrival_date, departure_date, today, reservation_index
            )

            # Compute nightly rate and total amount
            nightly_rate = _compute_nightly_rate(property_adr, room_type)
            total_amount = (nightly_rate * Decimal(str(stay_nights))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            # Group name (only for GROUP channel, non-event bookings)
            group_name = _assign_group_name(channel, reservation_index)

            # Special requests subset from guest preferences
            special_requests = _assign_special_requests(guest, reservation_index)

            # TTL: arrival date + 60 days
            ttl = _compute_ttl(arrival_date)

            # Reservation ID and sort key
            reservation_id = _generate_reservation_id(
                property_id, reservation_index + 1
            )
            date_reservation_id = f"{date_str}#{reservation_id}"

            reservation_item: Dict[str, Any] = {
                "propertyId": property_id,
                "dateReservationId": date_reservation_id,
                "arrivalDate": date_str,
                "departureDate": departure_date.isoformat(),
                "reservationId": reservation_id,
                "guestId": guest_id,
                "guestName": guest_name,
                "loyaltyTier": loyalty_tier,
                "roomNumber": room_number,
                "roomType": room_type,
                "status": status,
                "channel": channel,
                "rateCode": rate_code,
                "nightlyRate": nightly_rate,
                "totalAmount": total_amount,
                "stayNights": stay_nights,
                "groupName": group_name,
                "specialRequests": special_requests,
                "ttl": ttl,
            }

            daily_reservations.append(reservation_item)

        current_sequence += target_arrivals

    return daily_reservations, current_sequence


def generate_reservations(
    writer: BatchWriter,
    rooms_lookup: Dict[str, List[Dict[str, Any]]],
    guests_lookup: Dict[str, List[Dict[str, Any]]],
    revenue_lookup: Dict[Tuple[str, str], Dict[str, Any]],
    reference_date: Optional[Union[str, date]] = None,
    idempotent: bool = False,
) -> List[Dict[str, Any]]:
    """Generate reservations matching revenue occupancy targets for all properties.

    Produces ~45,000 reservation items across 5 properties over 30 days. For each
    property, first generates group event reservations from the event calendar,
    then generates daily arrival-based reservations matching the revenue arrival
    counts. All reservations reference valid rooms and guests from their respective
    lookup dicts.

    Args:
        writer: BatchWriter instance configured for the stayos-reservations table.
            Used to write generated items to DynamoDB in batches of 25.
        rooms_lookup: Dict keyed by propertyId mapping to lists of room dicts.
            Each room dict contains roomNumber, roomType, isPremiumRoom, etc.
            Generated by rooms_generator.generate_rooms().
        guests_lookup: Dict keyed by propertyId mapping to lists of guest dicts.
            Each guest dict contains guestId, name, loyaltyTier, preferences, etc.
            Generated by guests_generator.generate_guests().
        revenue_lookup: Dict keyed by (propertyId, date_str) tuples mapping to
            revenue item dicts containing arrivals, adr, occupiedRooms, etc.
            Generated by revenue_generator.generate_revenue().
        reference_date: The "today" the 30-day window and reservation statuses
            are anchored to, as an ISO YYYY-MM-DD string or a date. Defaults to
            UTC today when omitted so the whole window derives from this single
            value (Requirement 2.1). MUST match the value passed to
            generate_revenue() so reservation dates line up with revenue days.
        idempotent: When True, write via Idempotent_Upsert (put-if-changed,
            never delete) so a roll-forward re-run is a no-op (Requirements
            2.3, 2.4). When False (default), perform a plain full write.

    Returns:
        List of all generated reservation dicts across all properties.
        Needed by reconcile_room_status() to determine which rooms are OCCUPIED.
    """
    today = resolve_reference_date(reference_date)
    # Extend range by 2 days (today + tomorrow) to match revenue_generator's
    # extended window. Ensures arrival reservations exist for today/tomorrow
    # so the orchestrator always finds VIP arrivals.
    extra_days = 2
    total_days = SEED_DAYS + extra_days
    start_date = today - timedelta(days=SEED_DAYS - 1)

    all_reservations: List[Dict[str, Any]] = []

    for profile in PROPERTY_PROFILES:
        property_id: str = profile["propertyId"]

        rooms = rooms_lookup.get(property_id, [])
        guests = guests_lookup.get(property_id, [])

        if not rooms:
            logger.error(
                "No rooms found for %s - skipping reservation generation",
                property_id,
            )
            continue

        if not guests:
            logger.error(
                "No guests found for %s - skipping reservation generation",
                property_id,
            )
            continue

        logger.info(
            "Generating reservations for %s (%d rooms, %d guests available)",
            property_id,
            len(rooms),
            len(guests),
        )

        # Step 1: Generate group event reservations
        group_reservations, group_sequence_end = _generate_group_reservations(
            property_id=property_id,
            rooms=rooms,
            guests=guests,
            revenue_lookup=revenue_lookup,
            start_date=start_date,
            today=today,
            sequence_start=0,
        )

        # Step 2: Generate daily arrival-based reservations
        daily_reservations, daily_sequence_end = _generate_daily_reservations(
            property_id=property_id,
            rooms=rooms,
            guests=guests,
            revenue_lookup=revenue_lookup,
            start_date=start_date,
            today=today,
            profile=profile,
            sequence_start=group_sequence_end,
            total_days=total_days,
        )

        # Combine group and daily reservations for this property
        property_reservations = group_reservations + daily_reservations

        logger.info(
            "Generated %d reservations for %s (%d group, %d daily)",
            len(property_reservations),
            property_id,
            len(group_reservations),
            len(daily_reservations),
        )

        # Write all reservations for this property to DynamoDB
        result = writer.write_items(property_reservations, idempotent=idempotent)
        logger.info(
            "Reservations written for %s: %d succeeded, %d failed, %d skipped",
            property_id,
            result["success"],
            result["failed"],
            result["skipped"],
        )

        all_reservations.extend(property_reservations)

    total_generated = len(all_reservations)
    logger.info(
        "Total reservations generated: %d items across %d properties",
        total_generated,
        len(PROPERTY_PROFILES),
    )

    return all_reservations
