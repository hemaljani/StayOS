"""Work order lifecycle generator for the LUMI hotel dataset seeder.

Generates work orders with realistic creation-to-resolution lifecycles for each
of the 5 pilot properties over a 30-day window. Each work order progresses
through OPEN -> IN_PROGRESS -> RESOLVED based on its priority-determined
resolution time:

    - CRITICAL: resolved in 6-12 hours
    - HIGH: resolved in 12-24 hours
    - MEDIUM: resolved in 24-48 hours
    - LOW: resolved in 48-72 hours (scheduled maintenance)

The generator ensures 2-4 rooms are out-of-order (OOO) at any given time per
property by creating recent work orders with OPEN or IN_PROGRESS status in the
final days of the 30-day window.

Work orders reference valid room numbers from the rooms inventory and flag
premium rooms when affected (every 5th work order targets a premium room).

Volume target: ~750 total items (~150 per property over 30 days, averaging
~5 per day with day-to-day variance of 3-8).

Supports REQ-DS-6 (lifecycle-aware work orders), REQ-DS-9 (cross-table
consistency via valid room references), and REQ-DS-8 (deterministic generation).
"""

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from dataset_generator.config import (
    MAINTENANCE_TEAM,
    PROPERTY_PROFILES,
    RESOLUTION_TIME_HOURS,
    SEED_DAYS,
    TTL_WORK_ORDERS_DAYS,
    WORK_ORDER_CATEGORIES,
    WORK_ORDER_NOTES,
)
from dataset_generator.reference_date import resolve_reference_date
from dataset_generator.writer import BatchWriter

logger = logging.getLogger(__name__)

# Property short codes for work order ID generation.
# Pattern: WO-{SHORT_CODE}-{4-digit sequence}
PROPERTY_SHORT_CODES: Dict[str, str] = {
    "ALOHA-CHI-001": "CHI",
    "ALOHA-MIA-001": "MIA",
    "ALOHA-TYO-001": "TYO",
    "ALOHA-MAD-001": "MAD",
    "ALOHA-BOM-001": "BOM",
}

# Repeating priority pattern (20-item cycle) that ensures every group of
# ~5 work orders contains a mix of priorities. Distribution per cycle:
# 2 CRITICAL (10%), 5 HIGH (25%), 7 MEDIUM (35%), 6 LOW (30%)
# This guarantees recent work orders include HIGH/CRITICAL items that
# remain OPEN/IN_PROGRESS to satisfy the 2-4 OOO rooms requirement.
PRIORITY_CYCLE: List[str] = [
    "MEDIUM", "HIGH", "LOW", "MEDIUM", "CRITICAL",
    "HIGH", "MEDIUM", "LOW", "HIGH", "MEDIUM",
    "LOW", "MEDIUM", "HIGH", "LOW", "MEDIUM",
    "LOW", "HIGH", "CRITICAL", "LOW", "MEDIUM",
]

# Daily work order counts per property, rotating across 30 days.
# Values range 3-8 and average ~5 per day (150 per property over 30 days).
# The pattern ensures realistic daily variance in maintenance workload.
DAILY_WORK_ORDER_COUNTS: List[int] = [
    5, 4, 6, 5, 3, 7, 5,   # Week 1: mixed load
    4, 6, 5, 8, 4, 5, 5,   # Week 2: mid-week spike
    6, 5, 4, 5, 7, 4, 5,   # Week 3: end-of-week spike
    5, 6, 4, 5, 5, 7, 4,   # Week 4: steady
    6, 5,                    # Partial week 5
]


def _get_daily_count(day_index: int, property_index: int) -> int:
    """Get the number of work orders to generate for a specific day.

    Uses DAILY_WORK_ORDER_COUNTS with a property-specific offset to ensure
    different properties don't all spike on the same day.

    Args:
        day_index: Zero-based day index within the 30-day window (0-29).
        property_index: Zero-based index of the property (0-4).

    Returns:
        Integer count of work orders for this property on this day (3-8).
    """
    offset_index = (day_index + property_index * 3) % len(DAILY_WORK_ORDER_COUNTS)
    return DAILY_WORK_ORDER_COUNTS[offset_index]


def _determine_priority(work_order_index: int) -> str:
    """Determine work order priority based on index using a repeating cycle.

    Uses PRIORITY_CYCLE (20-item pattern) to ensure every group of ~5 work
    orders contains a mix of priorities. This guarantees HIGH/CRITICAL items
    appear among recent work orders to satisfy the OOO room constraint.

    Distribution per 20-item cycle: ~10% CRITICAL, ~25% HIGH, ~35% MEDIUM, ~30% LOW.

    Args:
        work_order_index: Global zero-based index of this work order
            within a property's full set.

    Returns:
        Priority string: "CRITICAL", "HIGH", "MEDIUM", or "LOW".
    """
    return PRIORITY_CYCLE[work_order_index % len(PRIORITY_CYCLE)]


def _compute_created_at(start_date: date, day_offset: int, work_order_index: int) -> datetime:
    """Compute the creation timestamp for a work order.

    Distributes work orders across business hours (6:00-22:00) using a
    deterministic formula based on the work order's index. Hour is computed
    as 6 + (index * 7) % 16, and minute as (index * 13) % 60.

    Args:
        start_date: The first day of the 30-day generation window.
        day_offset: Number of days from start_date for this work order's day.
        work_order_index: Global index of this work order within the property,
            used to determine hour and minute deterministically.

    Returns:
        Datetime representing the work order creation time.
    """
    # Hour distributed across 6:00-22:00 (16-hour window)
    hour = 6 + (work_order_index * 7) % 16
    minute = (work_order_index * 13) % 60

    creation_date = start_date + timedelta(days=day_offset)
    return datetime(
        creation_date.year,
        creation_date.month,
        creation_date.day,
        hour,
        minute,
        0,
    )


def _compute_resolution_hours(priority: str, work_order_index: int) -> int:
    """Compute the estimated resolution time in hours for a given priority.

    Uses RESOLUTION_TIME_HOURS ranges from config and selects a specific
    value deterministically within the range based on the work order index.

    Args:
        priority: Work order priority ("CRITICAL", "HIGH", "MEDIUM", "LOW").
        work_order_index: Global index used to pick a value within the range.

    Returns:
        Integer hours for estimated resolution time.
    """
    min_hours, max_hours = RESOLUTION_TIME_HOURS[priority]
    # Deterministic selection within the range
    hours_range = max_hours - min_hours
    selected_hours = min_hours + (work_order_index % (hours_range + 1))
    return selected_hours


def _determine_status(
    created_at: datetime,
    resolution_hours: int,
    reference_time: datetime,
) -> Tuple[str, Optional[datetime]]:
    """Determine work order status and resolved timestamp based on lifecycle timing.

    Status logic:
        - If created_at + resolution_hours <= reference_time: RESOLVED
        - If created_at + half_resolution <= reference_time < created_at + resolution: IN_PROGRESS
        - Otherwise: OPEN

    Args:
        created_at: When the work order was created.
        resolution_hours: Total expected hours to resolve (from priority).
        reference_time: Current reference time (end of generation window)
            used to determine lifecycle position.

    Returns:
        Tuple of (status_string, resolved_at_datetime_or_None).
        resolved_at is set only when status is "RESOLVED".
    """
    resolution_delta = timedelta(hours=resolution_hours)
    half_resolution_delta = timedelta(hours=resolution_hours / 2)

    resolved_at_time = created_at + resolution_delta
    in_progress_time = created_at + half_resolution_delta

    if resolved_at_time <= reference_time:
        # Work order has been resolved
        return "RESOLVED", resolved_at_time
    elif in_progress_time <= reference_time:
        # Work order is being actively worked on
        return "IN_PROGRESS", None
    else:
        # Work order was just created
        return "OPEN", None


def _select_room(
    rooms: List[Dict[str, Any]],
    work_order_index: int,
    used_room_indices: List[int],
) -> Tuple[str, bool]:
    """Select a room for a work order from the property's room inventory.

    Every 5th work order targets a premium room. Other work orders target
    non-premium rooms. Avoids assigning multiple concurrent work orders to
    the same room by tracking used indices.

    Args:
        rooms: List of room item dicts for the property from rooms_lookup.
        work_order_index: Global index of this work order, used for
            deterministic room selection and premium targeting.
        used_room_indices: List of room indices already assigned to active
            (non-resolved) work orders. Updated in place when a room is selected.

    Returns:
        Tuple of (room_number_string, is_premium_bool).
    """
    is_premium_target = (work_order_index % 5 == 0)

    if is_premium_target:
        # Filter to premium rooms not currently in use
        candidates = [
            (idx, room) for idx, room in enumerate(rooms)
            if room.get("isPremiumRoom", False) and idx not in used_room_indices
        ]
    else:
        # Filter to non-premium rooms not currently in use
        candidates = [
            (idx, room) for idx, room in enumerate(rooms)
            if not room.get("isPremiumRoom", False) and idx not in used_room_indices
        ]

    # Fallback: if no candidates in preferred category, use all available rooms
    if not candidates:
        candidates = [
            (idx, room) for idx, room in enumerate(rooms)
            if idx not in used_room_indices
        ]

    # Final fallback: if all rooms are used, allow reuse (unlikely with ~350+ rooms)
    if not candidates:
        candidates = [(idx, room) for idx, room in enumerate(rooms)]

    # Deterministic selection from candidates
    selected_idx = work_order_index % len(candidates)
    room_list_idx, selected_room = candidates[selected_idx]
    used_room_indices.append(room_list_idx)

    room_number: str = selected_room["roomNumber"]
    is_premium: bool = selected_room.get("isPremiumRoom", False)

    return room_number, is_premium


def _select_issue_type(work_order_index: int) -> Dict[str, str]:
    """Select an issue type by rotating through WORK_ORDER_CATEGORIES.

    Args:
        work_order_index: Global index for deterministic rotation.

    Returns:
        Dict with "issueType" and "defaultPriority" keys from the
        WORK_ORDER_CATEGORIES config pool.
    """
    category_index = work_order_index % len(WORK_ORDER_CATEGORIES)
    return WORK_ORDER_CATEGORIES[category_index]


def _select_notes(issue_type: str, work_order_index: int) -> str:
    """Select a description note for the work order.

    Rotates through WORK_ORDER_NOTES templates for the given issue type
    using the work order index.

    Args:
        issue_type: The work order's issue type (e.g., "HVAC", "PLUMBING").
        work_order_index: Global index for deterministic template rotation.

    Returns:
        String note/description for the work order.
    """
    notes_pool = WORK_ORDER_NOTES[issue_type]
    note_index = work_order_index % len(notes_pool)
    return notes_pool[note_index]


def _select_assigned_to(work_order_index: int) -> str:
    """Select a maintenance team member by rotating through MAINTENANCE_TEAM.

    Args:
        work_order_index: Global index for deterministic rotation.

    Returns:
        String name of the assigned maintenance team member.
    """
    member_index = work_order_index % len(MAINTENANCE_TEAM)
    return MAINTENANCE_TEAM[member_index]


def _compute_ttl(created_at: datetime) -> int:
    """Calculate DynamoDB TTL value for a work order.

    TTL is set to the creation datetime plus TTL_WORK_ORDERS_DAYS (60 days),
    converted to Unix epoch seconds.

    Args:
        created_at: The datetime when the work order was created.

    Returns:
        Integer Unix epoch timestamp for the TTL expiration.
    """
    expiry = created_at + timedelta(days=TTL_WORK_ORDERS_DAYS)
    return int(time.mktime(expiry.timetuple()))


def generate_work_orders(
    writer: BatchWriter,
    rooms_lookup: Dict[str, List[Dict[str, Any]]],
    reference_date: Optional[Union[str, date]] = None,
    idempotent: bool = False,
) -> List[Dict[str, Any]]:
    """Generate lifecycle-aware work orders for all 5 pilot properties.

    Creates 3-8 work orders per property per day over 30 days, producing
    approximately 750 total items (~150 per property). Each work order has a
    deterministic creation time, priority, resolution lifecycle, and references
    a valid room from the property's inventory.

    The lifecycle ensures 2-4 rooms are OOO at any given time per property.
    Recent work orders (last 1-3 days) naturally have OPEN/IN_PROGRESS status
    due to their resolution time not yet having elapsed, which satisfies the
    OOO room constraint.

    Args:
        writer: BatchWriter instance configured for the stayos-work-orders table.
            Used to write generated items to DynamoDB in batches of 25.
        rooms_lookup: Dict keyed by propertyId, where each value is a list of
            room item dicts from generate_rooms(). Used to assign valid room
            numbers and determine premium status.
        reference_date: The "today" the 30-day window and lifecycle status are
            anchored to, as an ISO YYYY-MM-DD string or a date. Defaults to UTC
            today when omitted so the whole window derives from this single
            value (Requirement 2.1).
        idempotent: When True, write via Idempotent_Upsert (put-if-changed,
            never delete) so a roll-forward re-run is a no-op (Requirements
            2.3, 2.4). When False (default), perform a plain full write.

    Returns:
        List of all generated work order dicts. Used by reconcile_room_status()
        to set OOO/MAINTENANCE status on affected rooms.
    """
    today = resolve_reference_date(reference_date)
    # 30-day window: from (today - 29 days) through today
    start_date = today - timedelta(days=SEED_DAYS - 1)
    # Reference time for lifecycle status: end of the reference date. Deriving
    # this from reference_date (instead of the wall-clock datetime.now()) keeps
    # generation fully deterministic (Requirement 2.5) so the same reference
    # date always yields the same statuses. End-of-day means every work order
    # created earlier today is already "created"; recent HIGH/CRITICAL orders
    # whose resolution window has not yet elapsed remain OPEN/IN_PROGRESS,
    # preserving the 2-4 OOO rooms guarantee.
    reference_time = datetime(today.year, today.month, today.day, 23, 59, 0)

    all_work_orders: List[Dict[str, Any]] = []

    for property_index, profile in enumerate(PROPERTY_PROFILES):
        property_id: str = profile["propertyId"]
        short_code: str = PROPERTY_SHORT_CODES[property_id]
        rooms: List[Dict[str, Any]] = rooms_lookup.get(property_id, [])

        if not rooms:
            logger.warning(
                "No rooms found for property %s - skipping work order generation",
                property_id,
            )
            continue

        logger.info(
            "Generating work orders for %s (%s)",
            property_id,
            short_code,
        )

        property_work_orders: List[Dict[str, Any]] = []
        work_order_sequence: int = 0
        # Track room indices with active (non-resolved) work orders
        # to avoid multiple concurrent issues on the same room
        active_room_indices: List[int] = []

        for day_index in range(SEED_DAYS):
            # Determine how many work orders to create this day (3-8)
            daily_count = _get_daily_count(day_index, property_index)

            for intra_day_index in range(daily_count):
                work_order_sequence += 1
                wo_index = work_order_sequence - 1

                # Build the work order ID: WO-CHI-0001
                work_order_id = f"WO-{short_code}-{work_order_sequence:04d}"

                # Determine issue type by rotating through categories
                category = _select_issue_type(wo_index)
                issue_type: str = category["issueType"]

                # Determine priority based on global index
                priority = _determine_priority(wo_index)

                # Compute creation timestamp within business hours
                created_at = _compute_created_at(start_date, day_index, wo_index)

                # Compute resolution time based on priority
                resolution_hours = _compute_resolution_hours(priority, wo_index)

                # Determine current status based on lifecycle timing
                status, resolved_at = _determine_status(
                    created_at, resolution_hours, reference_time
                )

                # Select a room (every 5th targets premium)
                room_number, is_premium = _select_room(
                    rooms, wo_index, active_room_indices
                )

                # If the work order is resolved, free up the room for future use
                if status == "RESOLVED":
                    # Remove the room index we just added since it's no longer active
                    if active_room_indices:
                        active_room_indices.pop()

                # Select description note from templates
                notes = _select_notes(issue_type, wo_index)

                # Assign maintenance team member
                assigned_to = _select_assigned_to(wo_index)

                # Format timestamps as ISO strings
                created_at_iso = created_at.strftime("%Y-%m-%dT%H:%M:%S")
                resolved_at_iso: Optional[str] = (
                    resolved_at.strftime("%Y-%m-%dT%H:%M:%S")
                    if resolved_at
                    else None
                )

                # Composite GSI sort key: status#createdAt
                status_created_at = f"{status}#{created_at_iso}"

                # TTL: createdAt + 60 days as Unix epoch
                ttl = _compute_ttl(created_at)

                work_order_item: Dict[str, Any] = {
                    "propertyId": property_id,
                    "workOrderId": work_order_id,
                    "statusCreatedAt": status_created_at,
                    "status": status,
                    "priority": priority,
                    "issueType": issue_type,
                    "roomNumber": room_number,
                    "isPremiumRoom": is_premium,
                    "notes": notes,
                    "assignedTo": assigned_to,
                    "createdAt": created_at_iso,
                    "resolvedAt": resolved_at_iso,
                    "estimatedResolutionHours": resolution_hours,
                    "ttl": ttl,
                }

                property_work_orders.append(work_order_item)

        logger.info(
            "Generated %d work orders for %s",
            len(property_work_orders),
            property_id,
        )

        # Write property work orders to DynamoDB
        result = writer.write_items(property_work_orders, idempotent=idempotent)
        logger.info(
            "Work orders written for %s: %d succeeded, %d failed, %d skipped",
            property_id,
            result["success"],
            result["failed"],
            result["skipped"],
        )

        all_work_orders.extend(property_work_orders)

    total_generated = len(all_work_orders)
    logger.info(
        "Total work orders generated: %d items across %d properties (target ~750)",
        total_generated,
        len(PROPERTY_PROFILES),
    )

    return all_work_orders
