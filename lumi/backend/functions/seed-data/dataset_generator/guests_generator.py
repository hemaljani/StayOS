"""VIP guest profile generator for the LUMI hotel dataset seeder.

Generates 50 VIP guest profiles per property (250 total) across the 5 pilot
properties. Each profile includes loyalty tier, preferences, corporate account,
contact information, and optional special occasion or sensitive notes.

Guest distribution per property:
    - 15 Ambassador (highest tier, 25-80 total stays)
    - 20 Titanium (mid-high tier, 10-40 total stays)
    - 15 Platinum (mid tier, 5-20 total stays)

All generation is deterministic (no randomness). Variance comes from index-based
modulo patterns and property-specific VIP name pools.

Supports REQ-DS-3 (50 VIPs per property), REQ-DS-8 (deterministic generation),
and REQ-DS-9 (cross-table consistency via guest lookup dict).
"""

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Union

from dataset_generator.config import (
    CORPORATE_ACCOUNTS,
    GUEST_PREFERENCES_POOL,
    LOYALTY_TIER_DISTRIBUTION,
    PROPERTY_PROFILES,
    SEED_DAYS,
    SENSITIVE_NOTES_POOL,
    SPECIAL_OCCASIONS_POOL,
    STAY_COUNT_RANGES,
    VIP_NAME_POOL,
)
from dataset_generator.reference_date import resolve_reference_date
from dataset_generator.writer import BatchWriter

logger = logging.getLogger(__name__)

# Property short codes for guestId generation (G-CHI-001, G-MIA-050, etc.)
PROPERTY_SHORT_CODES: Dict[str, str] = {
    "ALOHA-CHI-001": "CHI",
    "ALOHA-MIA-001": "MIA",
    "ALOHA-TYO-001": "TYO",
    "ALOHA-MAD-001": "MAD",
    "ALOHA-BOM-001": "BOM",
}

# Phone area codes by property for deterministic phone generation
PROPERTY_AREA_CODES: Dict[str, str] = {
    "ALOHA-CHI-001": "+1-312-555",
    "ALOHA-MIA-001": "+1-305-555",
    "ALOHA-TYO-001": "+81-3-5555",
    "ALOHA-MAD-001": "+34-91-555",
    "ALOHA-BOM-001": "+91-22-5555",
}


def _build_tier_sequence() -> List[str]:
    """Build an ordered sequence of loyalty tiers for 50 guests.

    Creates a flat list of 50 tier assignments in order:
    15 AMBASSADOR, then 20 TITANIUM, then 15 PLATINUM.
    Guest index 0-14 are Ambassador, 15-34 are Titanium, 35-49 are Platinum.

    Returns:
        List of 50 loyalty tier strings in assignment order.
    """
    sequence: List[str] = []
    # Iterate in the order defined by LOYALTY_TIER_DISTRIBUTION
    for tier, count in LOYALTY_TIER_DISTRIBUTION.items():
        sequence.extend([tier] * count)
    return sequence


# Precomputed tier sequence for all properties (same distribution per property)
_TIER_SEQUENCE: List[str] = _build_tier_sequence()


def _generate_guest_id(property_id: str, guest_index: int) -> str:
    """Generate a unique guest ID from property short code and sequence number.

    Pattern: G-{SHORT_CODE}-{3-digit sequence} (e.g., "G-CHI-001", "G-MIA-050").
    Sequence is 1-based (guest_index 0 produces "001").

    Args:
        property_id: The property identifier (e.g., "ALOHA-CHI-001").
        guest_index: Zero-based index of the guest within this property (0-49).

    Returns:
        Formatted guest ID string.
    """
    short_code = PROPERTY_SHORT_CODES[property_id]
    return f"G-{short_code}-{guest_index + 1:03d}"


def _compute_total_stays(loyalty_tier: str, guest_index: int) -> int:
    """Compute deterministic totalStays value based on tier range and guest index.

    Uses the formula: min + (guest_index * 7 % (max - min)) to distribute
    stay counts across the tier's range without randomness. The multiplier 7
    (a prime) ensures varied distribution even for consecutive indices.

    Args:
        loyalty_tier: One of AMBASSADOR, TITANIUM, or PLATINUM.
        guest_index: Zero-based index of the guest within the property (0-49).

    Returns:
        Integer total stays count within the tier's configured range.
    """
    min_stays, max_stays = STAY_COUNT_RANGES[loyalty_tier]
    stay_range = max_stays - min_stays
    # Use modulo to wrap within range; multiplier 7 spreads values
    offset = (guest_index * 7) % stay_range
    return min_stays + offset


def _assign_preferences(guest_index: int) -> List[str]:
    """Assign 1-5 preferences deterministically from the preferences pool.

    Number of preferences is determined by (guest_index % 5) + 1, giving
    a distribution of 1 to 5 preferences per guest. Preferences are selected
    using a sliding window starting at guest_index within the pool.

    Args:
        guest_index: Zero-based index of the guest within the property (0-49).

    Returns:
        List of 1-5 preference strings from GUEST_PREFERENCES_POOL.
    """
    # Number of preferences: cycles through 1, 2, 3, 4, 5
    num_preferences = (guest_index % 5) + 1
    pool_size = len(GUEST_PREFERENCES_POOL)

    # Sliding window: start position rotates through the pool
    start = guest_index % pool_size
    preferences: List[str] = []
    for i in range(num_preferences):
        idx = (start + i) % pool_size
        preferences.append(GUEST_PREFERENCES_POOL[idx])

    return preferences


def _assign_special_occasion(guest_index: int) -> Optional[str]:
    """Assign a special occasion to every 10th guest (10% of guests).

    Guests where index % 10 == 0 receive an occasion selected from the
    SPECIAL_OCCASIONS_POOL via modulo rotation.

    Args:
        guest_index: Zero-based index of the guest within the property (0-49).

    Returns:
        Occasion string if the guest qualifies (index % 10 == 0), else None.
    """
    if guest_index % 10 != 0:
        return None
    # Rotate through the occasions pool
    occasion_index = (guest_index // 10) % len(SPECIAL_OCCASIONS_POOL)
    return SPECIAL_OCCASIONS_POOL[occasion_index]


def _assign_sensitive_notes(guest_index: int) -> Optional[str]:
    """Assign sensitive notes to every 20th guest (5% of guests).

    Guests where index % 20 == 0 receive a note selected from the
    SENSITIVE_NOTES_POOL via modulo rotation.

    Args:
        guest_index: Zero-based index of the guest within the property (0-49).

    Returns:
        Sensitive note string if the guest qualifies (index % 20 == 0), else None.
    """
    if guest_index % 20 != 0:
        return None
    # Rotate through the sensitive notes pool
    note_index = (guest_index // 20) % len(SENSITIVE_NOTES_POOL)
    return SENSITIVE_NOTES_POOL[note_index]


def _assign_corporate_account(guest_index: int) -> str:
    """Assign a corporate account from the CORPORATE_ACCOUNTS pool.

    Deterministically assigned via modulo rotation through the pool.

    Args:
        guest_index: Zero-based index of the guest within the property (0-49).

    Returns:
        Corporate account name string.
    """
    return CORPORATE_ACCOUNTS[guest_index % len(CORPORATE_ACCOUNTS)]


def _derive_email(guest_name: str, corporate_account: str) -> str:
    """Derive an email address from the guest name and corporate account.

    Converts the guest name to lowercase with dots between parts, and
    generates a domain from the corporate account name (lowercase,
    hyphens for spaces, stripped of special characters).

    Examples:
        "David Chen" + "Meridian Corp" -> "david.chen@meridian-corp.com"
        "Maria Santos" + "Atlas Technologies" -> "maria.santos@atlas-technologies.com"

    Args:
        guest_name: Full name of the guest (e.g., "David Chen").
        corporate_account: Company name (e.g., "Meridian Corp").

    Returns:
        Email address string in the format name.parts@company-domain.com.
    """
    # Convert name: lowercase, replace spaces with dots
    name_part = guest_name.lower().replace(" ", ".")
    # Remove any special characters from name (apostrophes, hyphens in names)
    name_part = "".join(c for c in name_part if c.isalpha() or c == ".")

    # Convert company: lowercase, replace spaces with hyphens, remove special chars
    domain_part = corporate_account.lower().replace(" ", "-")
    domain_part = "".join(c for c in domain_part if c.isalpha() or c == "-")

    return f"{name_part}@{domain_part}.com"


def _generate_phone(property_id: str, guest_index: int) -> str:
    """Generate a deterministic phone number using property area code and index.

    Format: {area_code}-{4-digit suffix}. The suffix is derived from the
    guest index to ensure uniqueness within each property.

    Args:
        property_id: The property identifier (e.g., "ALOHA-CHI-001").
        guest_index: Zero-based index of the guest within the property (0-49).

    Returns:
        Phone number string (e.g., "+1-312-555-0101").
    """
    area_code = PROPERTY_AREA_CODES[property_id]
    # 4-digit suffix starting from 0101, incrementing per guest
    suffix = f"{guest_index + 101:04d}"
    return f"{area_code}-{suffix}"


def _compute_last_stay_date(guest_index: int, base_date: date) -> str:
    """Compute a deterministic lastStayDate within the last 30 days.

    Uses the formula: base_date - timedelta(days=index % 30) to spread
    last-stay dates across the 30-day seed window.

    Args:
        guest_index: Zero-based index of the guest within the property (0-49).
        base_date: The reference "today" date for the dataset (typically today).

    Returns:
        ISO-format date string (e.g., "2026-07-25").
    """
    days_ago = guest_index % SEED_DAYS
    stay_date = base_date - timedelta(days=days_ago)
    return stay_date.isoformat()


def generate_guests(
    writer: BatchWriter,
    reference_date: Optional[Union[str, date]] = None,
    idempotent: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Generate 50 VIP guest profiles per property (250 total) and write to DynamoDB.

    Iterates over all PROPERTY_PROFILES and for each property generates 50
    guests: 15 Ambassador, 20 Titanium, 15 Platinum. Each guest receives
    deterministically assigned attributes including loyalty tier, preferences,
    corporate account, contact information, and optional occasion/notes.

    The returned lookup dict maps propertyId to guest lists for use by the
    reservations generator to assign valid guestIds to bookings.

    Args:
        writer: BatchWriter instance configured for the stayos-guests table.
            Used to write generated items to DynamoDB in batches of 25.
        reference_date: The "today" used to anchor lastStayDate calculations,
            as an ISO YYYY-MM-DD string or a date. Defaults to UTC today when
            omitted so the whole window derives from this value (Requirement 2.1).
        idempotent: When True, write via Idempotent_Upsert (put-if-changed,
            never delete) so a roll-forward re-run is a no-op (Requirements
            2.3, 2.4). When False (default), perform a plain full write.

    Returns:
        Dict keyed by propertyId, where each value is a list of 50 guest
        item dicts. Each guest dict contains all DynamoDB attributes.
        Structure: Dict[str, List[Dict[str, Any]]]
    """
    # Anchor lastStayDate calculations to the resolved reference date
    base_date = resolve_reference_date(reference_date)
    guests_lookup: Dict[str, List[Dict[str, Any]]] = {}

    for profile in PROPERTY_PROFILES:
        property_id: str = profile["propertyId"]
        name_pool: List[str] = VIP_NAME_POOL[property_id]

        logger.info(
            "Generating 50 VIP guest profiles for %s", property_id
        )

        property_guests: List[Dict[str, Any]] = []

        for guest_index in range(50):
            # Core identity
            guest_id = _generate_guest_id(property_id, guest_index)
            guest_name = name_pool[guest_index]
            loyalty_tier = _TIER_SEQUENCE[guest_index]

            # Derived attributes
            total_stays = _compute_total_stays(loyalty_tier, guest_index)
            preferences = _assign_preferences(guest_index)
            special_occasion = _assign_special_occasion(guest_index)
            sensitive_notes = _assign_sensitive_notes(guest_index)
            corporate_account = _assign_corporate_account(guest_index)
            email = _derive_email(guest_name, corporate_account)
            phone = _generate_phone(property_id, guest_index)
            last_stay_date = _compute_last_stay_date(guest_index, base_date)

            # Composite sort key for GSI: loyaltyTier#guestId
            loyalty_tier_guest_id = f"{loyalty_tier}#{guest_id}"

            guest_item: Dict[str, Any] = {
                "propertyId": property_id,
                "guestId": guest_id,
                "loyaltyTierGuestId": loyalty_tier_guest_id,
                "name": guest_name,
                "loyaltyTier": loyalty_tier,
                "totalStays": total_stays,
                "preferences": preferences,
                "specialOccasion": special_occasion,
                "sensitiveNotes": sensitive_notes,
                "corporateAccount": corporate_account,
                "email": email,
                "phone": phone,
                "lastStayDate": last_stay_date,
            }

            property_guests.append(guest_item)

        # Write all 50 guests for this property to DynamoDB
        result = writer.write_items(property_guests, idempotent=idempotent)
        logger.info(
            "Guest profiles written for %s: %d succeeded, %d failed, %d skipped",
            property_id,
            result["success"],
            result["failed"],
            result["skipped"],
        )

        guests_lookup[property_id] = property_guests

    total_generated = sum(len(guests) for guests in guests_lookup.values())
    logger.info(
        "Total guest profiles generated: %d items across %d properties",
        total_generated,
        len(guests_lookup),
    )

    return guests_lookup
