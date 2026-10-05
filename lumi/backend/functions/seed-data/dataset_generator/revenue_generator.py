"""Daily revenue and KPI snapshot generator for the LUMI hotel dataset seeder.

Generates 30 days of daily revenue/KPI records for each of the 5 pilot properties
(150 total items). Each record represents a single property-day combination with
occupancy, ADR, RevPAR, revenue, comparison deltas, segment mix, and upsell metrics.

Occupancy patterns reflect property profiles:
    - Chicago: business weekday peak (Mon-Thu 85-92%, Fri-Sun 72-80%)
    - Miami: leisure weekend peak (Fri-Sun 85-92%, Mon-Thu 75-82%)
    - Tokyo: consistently high (88-96%, low variance)
    - Madrid: mid-week dip (Wednesday lowest, weekend recovery)
    - Mumbai: growing trend (steady upward slope over 30 days)

All generation is deterministic (no randomness). Day-specific variance comes from
OCCUPANCY_OFFSETS and ADR_OFFSETS arrays in config. Comparison deltas (vsLastWeek,
vsBudget, vsYOY) are derived from actual generated values, not independently set.

Supports REQ-DS-4 (30-day realistic revenue patterns), REQ-DS-8 (deterministic
generation), and REQ-DS-10 (agent queryability via date-range queries).
"""

import logging
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from dataset_generator.config import (
    ADR_OFFSETS,
    OCCUPANCY_OFFSETS,
    PROPERTY_PROFILES,
    SEED_DAYS,
    SEGMENT_MIX,
    TTL_REVENUES_DAYS,
)
from dataset_generator.reference_date import resolve_reference_date
from dataset_generator.writer import BatchWriter

logger = logging.getLogger(__name__)

# Average stay length used to estimate arrivals/departures from occupied rooms.
# Business-weighted properties average ~2.3 nights, leisure ~3.1 nights.
# We use a fixed 2.5-night average for simplicity across all properties.
AVERAGE_STAY_LENGTH_NIGHTS: Decimal = Decimal("2.5")


def _get_base_occupancy(profile: Dict[str, Any], record_date: date) -> Decimal:
    """Determine base occupancy percentage from day-of-week and property profile.

    Weekday (Monday-Friday) uses the midpoint of weekdayOccupancy range.
    Weekend (Saturday-Sunday) uses the midpoint of weekendOccupancy range.

    Args:
        profile: Property profile dict from PROPERTY_PROFILES containing
            weekdayOccupancy and weekendOccupancy tuples (min_pct, max_pct).
        record_date: The specific date to determine base occupancy for.

    Returns:
        Decimal midpoint of the applicable occupancy range for this day type.
    """
    # Monday=0 through Sunday=6 in Python's weekday() convention
    day_of_week = record_date.weekday()

    if day_of_week < 5:
        # Weekday: Monday(0) through Friday(4)
        min_occ, max_occ = profile["weekdayOccupancy"]
    else:
        # Weekend: Saturday(5) and Sunday(6)
        min_occ, max_occ = profile["weekendOccupancy"]

    # Midpoint of the occupancy range
    midpoint = Decimal(str((min_occ + max_occ) / 2))
    return midpoint


def _compute_occupancy_pct(
    profile: Dict[str, Any],
    record_date: date,
    day_index: int,
) -> Decimal:
    """Compute final occupancy percentage for a property on a specific day.

    Combines the base occupancy (weekday/weekend midpoint) with the
    property-specific day offset from OCCUPANCY_OFFSETS to produce
    realistic day-to-day variance. Clamps result between 0 and 100.

    Args:
        profile: Property profile dict containing occupancy range tuples.
        record_date: The date this revenue record represents.
        day_index: Zero-based day index (0 = oldest day, 29 = today).
            Used to look up the offset in OCCUPANCY_OFFSETS array.

    Returns:
        Decimal occupancy percentage, clamped to [0, 100].
    """
    property_id: str = profile["propertyId"]
    base = _get_base_occupancy(profile, record_date)

    # Apply day-specific offset from config
    offset = OCCUPANCY_OFFSETS[property_id][day_index]
    occupancy = base + Decimal(str(offset))

    # Clamp between 0 and 100
    occupancy = max(Decimal("0"), min(Decimal("100"), occupancy))

    # Round to one decimal place for clean KPI display
    return occupancy.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _compute_adr(profile: Dict[str, Any], day_index: int) -> Decimal:
    """Compute the Average Daily Rate for a property on a specific day.

    ADR is the property baseline plus a day-specific offset that correlates
    with demand (higher on peak occupancy days, lower on soft days).

    Args:
        profile: Property profile dict containing adrBaseline (Decimal).
        day_index: Zero-based day index for offset lookup in ADR_OFFSETS.

    Returns:
        Decimal ADR value, minimum of 0 (prevents negative rates).
    """
    property_id: str = profile["propertyId"]
    baseline: Decimal = profile["adrBaseline"]

    # Apply day-specific ADR offset from config
    offset = ADR_OFFSETS[property_id][day_index]
    adr = baseline + Decimal(str(offset))

    # Ensure non-negative rate
    return max(Decimal("0"), adr.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _compute_occupied_rooms(total_rooms: int, occupancy_pct: Decimal) -> int:
    """Calculate number of occupied rooms from occupancy percentage.

    Args:
        total_rooms: Total room inventory for the property.
        occupancy_pct: Occupancy percentage (e.g., Decimal("87.5")).

    Returns:
        Integer number of occupied rooms, rounded to nearest whole number.
    """
    occupied = Decimal(str(total_rooms)) * occupancy_pct / Decimal("100")
    return int(occupied.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _compute_revpar(occupancy_pct: Decimal, adr: Decimal) -> Decimal:
    """Derive RevPAR from occupancy percentage and ADR.

    RevPAR (Revenue Per Available Room) = occupancyPct/100 * ADR.
    This is the standard hotel KPI formula.

    Args:
        occupancy_pct: Occupancy percentage (e.g., Decimal("87.5")).
        adr: Average Daily Rate (e.g., Decimal("258.00")).

    Returns:
        Decimal RevPAR value rounded to 2 decimal places.
    """
    revpar = (occupancy_pct / Decimal("100")) * adr
    return revpar.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _compute_total_revenue(adr: Decimal, occupied_rooms: int) -> Decimal:
    """Calculate total daily revenue from ADR and occupied room count.

    Args:
        adr: Average Daily Rate for the day.
        occupied_rooms: Number of rooms occupied on the day.

    Returns:
        Decimal total revenue (ADR * occupied rooms), rounded to 2 decimals.
    """
    revenue = adr * Decimal(str(occupied_rooms))
    return revenue.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _compute_vs_last_week(
    current_occupancy: Decimal,
    day_index: int,
    occupancy_history: List[Decimal],
) -> Decimal:
    """Calculate occupancy delta versus the same day one week ago.

    For the first 7 days (day_index 0-6), there is no prior-week reference,
    so the delta is 0. For day_index >= 7, the delta is current minus the
    value at day_index - 7.

    Args:
        current_occupancy: Today's occupancy percentage.
        day_index: Zero-based index of the current day in the 30-day window.
        occupancy_history: List of already-computed occupancy values for
            all days up to but not including the current day.

    Returns:
        Decimal percentage-point difference vs last week, rounded to 1 decimal.
    """
    if day_index < 7:
        return Decimal("0.0")

    last_week_occupancy = occupancy_history[day_index - 7]
    delta = current_occupancy - last_week_occupancy
    return delta.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _compute_vs_budget(
    current_occupancy: Decimal,
    budget_occupancy: int,
) -> Decimal:
    """Calculate occupancy delta versus the property's budget target.

    The budget target is a fixed percentage from the property profile
    (budgetOccupancy field). Delta is current minus budget.

    Args:
        current_occupancy: Today's occupancy percentage.
        budget_occupancy: Property's annual budget occupancy target (integer %).

    Returns:
        Decimal percentage-point difference vs budget, rounded to 1 decimal.
    """
    delta = current_occupancy - Decimal(str(budget_occupancy))
    return delta.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _compute_vs_yoy(day_index: int) -> Decimal:
    """Compute a deterministic year-over-year occupancy delta.

    Simulates YOY comparison using a formula: (day_index * 3) % 7 - 3.
    This produces values in the range [-3, +3] with realistic variance,
    simulating that last year's same day was slightly different.

    Args:
        day_index: Zero-based day index (0 = oldest day, 29 = today).

    Returns:
        Decimal YOY delta in percentage points, rounded to 1 decimal.
    """
    yoy_offset = (day_index * 3) % 7 - 3
    return Decimal(str(yoy_offset)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _compute_arrivals(occupied_rooms: int) -> int:
    """Estimate daily arrivals from occupied room count and average stay length.

    Arrivals represent new check-ins for the day. Estimated as occupied rooms
    divided by average stay length, reflecting the natural turnover rate.

    Args:
        occupied_rooms: Number of occupied rooms on this day.

    Returns:
        Integer estimated arrivals (new check-ins today).
    """
    arrivals = Decimal(str(occupied_rooms)) / AVERAGE_STAY_LENGTH_NIGHTS
    return int(arrivals.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _compute_departures(occupied_rooms: int, day_index: int) -> int:
    """Estimate daily departures with slight day-to-day variance.

    Departures are similar to arrivals in magnitude but offset slightly
    to create realistic flow (not every day has equal in/out). Uses a
    small deterministic adjustment based on day_index.

    Args:
        occupied_rooms: Number of occupied rooms on this day.
        day_index: Zero-based day index for minor deterministic variance.

    Returns:
        Integer estimated departures (check-outs today).
    """
    # Base departures same as arrivals formula
    base_departures = Decimal(str(occupied_rooms)) / AVERAGE_STAY_LENGTH_NIGHTS
    # Small deterministic variance: +/- a few based on day_index
    variance = Decimal(str((day_index % 5) - 2))
    departures = base_departures + variance
    # Ensure non-negative
    return max(0, int(departures.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _compute_upsell_metrics(arrivals: int) -> Tuple[int, int]:
    """Calculate upsell eligible and converted counts from arrivals.

    Eligible guests are ~15% of arrivals (standard room guests who could
    be upgraded). Conversion rate is ~33% of eligible guests.

    Args:
        arrivals: Number of new check-ins today.

    Returns:
        Tuple of (upsell_eligible, upsell_converted) integer counts.
    """
    eligible = round(arrivals * 0.15)
    converted = round(eligible * 0.33)
    return eligible, converted


def _compute_ttl(record_date: date) -> int:
    """Calculate DynamoDB TTL value for a revenue record.

    TTL is set to the record date plus TTL_REVENUES_DAYS (90 days),
    converted to Unix epoch seconds.

    Args:
        record_date: The date this revenue record represents.

    Returns:
        Integer Unix epoch timestamp for the TTL expiration.
    """
    expiry_date = record_date + timedelta(days=TTL_REVENUES_DAYS)
    # Convert date to epoch: midnight UTC on the expiry date
    epoch = int(time.mktime(expiry_date.timetuple()))
    return epoch


def generate_revenue(
    writer: BatchWriter,
    reference_date: Optional[Union[str, date]] = None,
    idempotent: bool = False,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Generate 30 days of daily revenue/KPI records for all 5 pilot properties.

    Iterates over PROPERTY_PROFILES and for each property generates one revenue
    item per day for SEED_DAYS (30) consecutive days ending on the reference
    date. Revenue items include occupancy, ADR, RevPAR, total revenue,
    comparison deltas, segment mix, and upsell metrics.

    The returned lookup dict maps (propertyId, date_str) tuples to revenue
    items, enabling the reservations generator to match occupancy targets
    when generating reservation volumes.

    Args:
        writer: BatchWriter instance configured for the stayos-revenues table.
            Used to write generated items to DynamoDB in batches of 25.
        reference_date: The "today" the 30-day window is anchored to, as an ISO
            YYYY-MM-DD string or a date. Defaults to UTC today when omitted so
            the whole window derives from this single value (Requirement 2.1).
        idempotent: When True, write via Idempotent_Upsert (put-if-changed,
            never delete) so a roll-forward re-run is a no-op (Requirements
            2.3, 2.4). When False (default), perform a plain full write.

    Returns:
        Dict keyed by (propertyId, date_str) tuples, where each value is
        the full revenue item dict for that property-day combination.
        Structure: Dict[Tuple[str, str], Dict[str, Any]]
    """
    today = resolve_reference_date(reference_date)
    # Generate from (today - 29 days) through tomorrow (32 days total).
    # The extra 2 days (today + tomorrow) ensure the orchestrator always finds
    # revenue data for today regardless of when the seeder last ran.
    extra_days = 2
    total_days = SEED_DAYS + extra_days
    start_date = today - timedelta(days=SEED_DAYS - 1)

    revenue_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    all_items: List[Dict[str, Any]] = []

    for profile in PROPERTY_PROFILES:
        property_id: str = profile["propertyId"]
        total_rooms: int = profile["totalRooms"]
        budget_occupancy: int = profile["budgetOccupancy"]

        logger.info(
            "Generating %d days of revenue data for %s (includes +%d future days)",
            total_days,
            property_id,
            extra_days,
        )

        # Track occupancy history for vsLastWeek calculation
        occupancy_history: List[Decimal] = []

        for day_index in range(total_days):
            record_date = start_date + timedelta(days=day_index)
            date_str = record_date.isoformat()

            # Core KPIs
            # Wrap day_index for offset array lookup when generating beyond SEED_DAYS
            offset_index = day_index % SEED_DAYS
            occupancy_pct = _compute_occupancy_pct(profile, record_date, offset_index)
            adr = _compute_adr(profile, offset_index)
            occupied_rooms = _compute_occupied_rooms(total_rooms, occupancy_pct)
            revpar = _compute_revpar(occupancy_pct, adr)
            total_revenue = _compute_total_revenue(adr, occupied_rooms)

            # Comparison deltas
            vs_last_week = _compute_vs_last_week(
                occupancy_pct, day_index, occupancy_history
            )
            vs_budget = _compute_vs_budget(occupancy_pct, budget_occupancy)
            vs_yoy = _compute_vs_yoy(offset_index)

            # Arrivals and departures
            arrivals = _compute_arrivals(occupied_rooms)
            departures = _compute_departures(occupied_rooms, offset_index)

            # Confirmed reservations include 3% oversell margin
            confirmed_reservations = occupied_rooms + round(occupied_rooms * 0.03)

            # Upsell metrics
            upsell_eligible, upsell_converted = _compute_upsell_metrics(arrivals)

            # Segment mix from config (same per property every day)
            segment_mix = SEGMENT_MIX[property_id]

            # TTL for DynamoDB auto-deletion (record date + 90 days)
            ttl = _compute_ttl(record_date)

            revenue_item: Dict[str, Any] = {
                "propertyId": property_id,
                "date": date_str,
                "occupancyPct": occupancy_pct,
                "adr": adr,
                "revpar": revpar,
                "totalRevenue": total_revenue,
                "availableRooms": total_rooms,
                "occupiedRooms": occupied_rooms,
                "confirmedReservations": confirmed_reservations,
                "arrivals": arrivals,
                "departures": departures,
                "vsLastWeek": vs_last_week,
                "vsBudget": vs_budget,
                "vsYOY": vs_yoy,
                "segmentMix": segment_mix,
                "upsellEligible": upsell_eligible,
                "upsellConverted": upsell_converted,
                "ttl": ttl,
            }

            all_items.append(revenue_item)
            revenue_lookup[(property_id, date_str)] = revenue_item

            # Record occupancy for vsLastWeek lookback by future days
            occupancy_history.append(occupancy_pct)

        logger.info(
            "Generated %d revenue items for %s", total_days, property_id
        )

    # Write all revenue items to DynamoDB
    result = writer.write_items(all_items, idempotent=idempotent)
    logger.info(
        "Revenue data written: %d succeeded, %d failed, %d skipped (%d expected)",
        result["success"],
        result["failed"],
        result["skipped"],
        len(PROPERTY_PROFILES) * total_days,
    )

    return revenue_lookup
