"""Situation context passed to the Triage Agent for a single alert.

A :class:`SituationContext` carries the structured, per-alert-type facts the
Triage Agent needs to (a) build a Bedrock prompt and (b) construct the
deterministic, structurally-guaranteed parts of a brief (the Walk_Strategy,
replacement-room options, and so on) rather than trusting the model for
safety-critical structure. The caller (the delivery/rule path or the triage
Lambda handler) assembles this from the alert plus SPOG lookups.

External lookups are expressed as **seams** -- plain callables -- so the pure
specialization logic stays testable without SPOG or the network:

    * ``sister_property_lookup`` selects a sister property with availability for
      the required stay dates (Walk Risk, Requirement 3.4).

Loyalty tiers are compared by an integer rank (higher rank = more elite). The
default ordering in :data:`LOYALTY_RANK` reflects the tiers referenced in the
requirements; a guest at or below the configured protection rank is walkable
(Requirement 3.3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

# Loyalty-tier ranking (higher = more elite). "Ambassador" is the highest tier
# referenced by the escalation rules (see requirements Glossary). A guest whose
# rank is at or below the configured protection rank is eligible to be walked.
LOYALTY_RANK: dict[str, int] = {
    "Member": 0,
    "Silver": 1,
    "Gold": 2,
    "Platinum": 3,
    "Ambassador": 4,
}

# A sister-property lookup takes the required stay dates (start, end) and returns
# the id of a sister property with availability, or None when none is available.
SisterPropertyLookup = Callable[[tuple[str, str]], Optional[str]]


def loyalty_rank(tier: Optional[str], default: int = 0) -> int:
    """Return the integer rank for a loyalty tier.

    Args:
        tier: The loyalty tier name, or ``None``.
        default: The rank to use for an unknown/absent tier.

    Returns:
        The tier's rank (higher = more elite), or ``default`` when unknown.
    """
    if tier is None:
        return default
    return LOYALTY_RANK.get(tier, default)


@dataclass
class SituationContext:
    """Structured, per-alert-type facts supplied to the Triage Agent.

    All fields are optional with safe defaults so a caller only populates the
    facts relevant to the alert type being triaged. The Bedrock prompt renders
    from these facts; the specialization builders consume the same facts to
    construct structurally-guaranteed brief content.

    Attributes:
        property_id: The property the alert belongs to.
        currency: The property's configured currency code (for costs).
        confirmed_guests: Walk Risk - confirmed guests, each a mapping with
            ``guestId``, ``reservationId``, and ``loyaltyTier``.
        room_shortfall: Walk Risk - the number of rooms oversold (confirmed
            minus available), i.e. the walkable-guest cap.
        loyalty_protection_tier: Walk Risk - the configured protection tier; a
            guest at or below this tier's rank is walkable.
        stay_dates: Walk Risk - the required stay dates as ``(start, end)`` ISO
            date strings, passed to ``sister_property_lookup``.
        sister_property_lookup: Walk Risk - seam selecting a sister property.
        required_room_type: OOO Cluster - the affected group block's room type.
        replacement_candidates: OOO Cluster - candidate replacement rooms, each
            a mapping with ``roomId``, ``roomType``, ``availableForRange``, and
            ``suitability``.
        group_block: OOO Cluster - the affected group block descriptor.
        vip_rush_clean: VIP Room Not Ready - the rush-clean option descriptor.
        vip_room_move: VIP Room Not Ready - the room-move option descriptor.
        complaint_option_candidates: Complaint - candidate remedy options, each
            with ``title``, ``detail``, ``estimatedCost``, ``reviewRisk``, and an
            optional ``recommended`` flag.
        extra: Free-form additional facts for prompt rendering.
    """

    property_id: str
    currency: str = "USD"

    # Walk Risk (UC-01)
    confirmed_guests: list[dict[str, Any]] = field(default_factory=list)
    room_shortfall: int = 0
    loyalty_protection_tier: Optional[str] = None
    stay_dates: Optional[tuple[str, str]] = None
    sister_property_lookup: Optional[SisterPropertyLookup] = None

    # OOO Cluster (UC-03)
    required_room_type: Optional[str] = None
    replacement_candidates: list[dict[str, Any]] = field(default_factory=list)
    group_block: Optional[dict[str, Any]] = None

    # VIP Room Not Ready (UC-02)
    vip_rush_clean: Optional[dict[str, Any]] = None
    vip_room_move: Optional[dict[str, Any]] = None

    # Guest Complaint Escalation (UC-04)
    complaint_option_candidates: list[dict[str, Any]] = field(default_factory=list)

    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "LOYALTY_RANK",
    "SisterPropertyLookup",
    "loyalty_rank",
    "SituationContext",
]
