"""Per-alert-type triage specializations (pure, structurally-guaranteed logic).

The Triage Agent uses Amazon Bedrock for narrative reasoning (the summary and
option prose), but the *structural* guarantees each alert type must satisfy are
enforced here in deterministic, I/O-free Python rather than trusted to the
model. That keeps the safety-relevant invariants (the walkable cap and loyalty
threshold, the complaint option bounds, the replacement-room matching) provably
correct and unit-testable at 100+ iterations.

Specializations implemented:
    * Walk Risk (UC-01): :func:`build_walk_strategy` selects walkable guests
      (loyalty at or below the protection threshold, capped at the room
      shortfall), one compensation package each, and a sister property via the
      lookup seam - marking the assignment unavailable when none is found
      (Requirements 3.3-3.6, Property 6).
    * VIP Room Not Ready (UC-02): :func:`build_vip_options` returns at least one
      rush-clean and one room-move option at distinct ranks (Requirement 4.2).
    * Guest Complaint Escalation (UC-04): :func:`build_complaint_options`
      returns 3-5 options, each with a numeric estimated cost and a review-risk
      level, exactly one recommended, and raises when fewer than 3 well-formed
      options exist so the caller can notify the GM that manual resolution is
      required (Requirements 5.2, 5.4, Property 10).
    * OOO Cluster (UC-03): :func:`build_ooo_replacement_options` returns up to 5
      type-matched, fully-available replacement rooms ordered by suitability
      (zero when none), plus :func:`draft_group_notification` (Requirements
      7.2-7.4, Property 14).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from string import ascii_uppercase
from typing import Any

from pulse.common.errors import TriageFailure
from pulse.common.logging import get_logger
from pulse.common.models import (
    CompensationPackage,
    RankedOption,
    ReviewRisk,
    WalkableGuest,
    WalkStrategy,
)
from pulse.triage.context import SituationContext, loyalty_rank

logger = get_logger("pulse-triage-agent")

# Complaint option-count bounds (Requirement 5.2).
COMPLAINT_MIN_OPTIONS = 3
COMPLAINT_MAX_OPTIONS = 5

# Maximum replacement rooms presented for an OOO cluster (Requirement 7.2).
OOO_MAX_REPLACEMENTS = 5

# Default per-guest compensation value (property currency) used when a caller
# does not supply a bespoke package. A prototype-friendly flat package.
DEFAULT_COMPENSATION_COST = 150.0


def _label_for(index: int) -> str:
    """Return the option label for a 0-based index (A, B, C, ...).

    Args:
        index: The 0-based option index.

    Returns:
        A single-letter label; wraps to numbered labels beyond 26 options
        (never reached given the 5-option maximum, but kept safe).
    """
    if index < len(ascii_uppercase):
        return ascii_uppercase[index]
    return f"OPT{index + 1}"


# ---------------------------------------------------------------------------
# UC-01: Walk Strategy
# ---------------------------------------------------------------------------


def _default_compensation(
    guest: Mapping[str, Any], currency: str
) -> CompensationPackage:
    """Build a default compensation package for a walkable guest.

    Args:
        guest: The walkable guest mapping (``guestId``).
        currency: The property currency code (for the description).

    Returns:
        A :class:`CompensationPackage` for the guest.
    """
    guest_id = str(guest.get("guestId"))
    return CompensationPackage(
        guest_id=guest_id,
        description=(
            f"Walk to sister property with paid transfer and one-night rate "
            f"match ({currency})."
        ),
        estimated_cost=DEFAULT_COMPENSATION_COST,
    )


def build_walk_strategy(context: SituationContext) -> WalkStrategy:
    """Build a Walk_Strategy for a Walk Risk alert (Requirements 3.3-3.5).

    Selects walkable guests as those whose loyalty tier rank is at or below the
    configured protection tier's rank, ordered least-elite first, and capped at
    the room shortfall count (Property 6). Each selected guest receives exactly
    one compensation package.

    Option B (demo-honest reframe): the strategy no longer recommends relocating
    guests to a same-brand "sister" property. The pilot estate is one hotel per
    city, so a same-brand property is in a different city/country and walking a
    guest there is not a realistic GM action. The relocation is instead framed
    (in the narrative options + the prompt) as in-house actions first (protect
    elite tiers, upgrade in-house, hold/monitor) with a generic external
    partner-overflow as a last resort. The ``sister_property_*`` fields are
    therefore always empty/False here (kept only for payload/schema stability).

    Args:
        context: The situation context carrying the confirmed guests, the room
            shortfall, and the protection tier.

    Returns:
        A :class:`WalkStrategy` whose walkable-guest count never exceeds the
        shortfall and whose guests are all at or below the protection tier. No
        sister property is ever assigned.
    """
    protection_rank = loyalty_rank(context.loyalty_protection_tier)
    cap = max(context.room_shortfall, 0)

    # Eligible = loyalty rank at or below the protection threshold. Order by
    # rank ascending (walk the least-elite guests first) for a stable, fair
    # selection, then cap at the shortfall.
    eligible = [
        guest
        for guest in context.confirmed_guests
        if loyalty_rank(guest.get("loyaltyTier")) <= protection_rank
    ]
    eligible.sort(key=lambda guest: loyalty_rank(guest.get("loyaltyTier")))
    selected = eligible[:cap]

    walkable_guests: list[WalkableGuest] = [
        WalkableGuest(
            guest_id=str(guest.get("guestId")),
            loyalty_tier=guest.get("loyaltyTier"),
            reservation_id=str(guest.get("reservationId")),
        )
        for guest in selected
    ]
    compensation = [
        _default_compensation(guest, context.currency) for guest in selected
    ]

    # No cross-city sister-property recommendation (Option B). Relocation is
    # handled as in-house / generic partner-overflow in the ranked options.
    return WalkStrategy(
        sister_property_id=None,
        sister_property_available=False,
        walkable_guests=walkable_guests,
        compensation=compensation,
    )


# ---------------------------------------------------------------------------
# UC-02: VIP Room Not Ready options
# ---------------------------------------------------------------------------


def build_vip_options(context: SituationContext) -> list[RankedOption]:
    """Build VIP Room Not Ready options (Requirement 4.2).

    Returns at least one rush-clean option and one room-move option, each at a
    distinct rank position. The rush-clean is presented first (rank 1,
    recommended) and the room-move second (rank 2); a caller may override the
    descriptors via the context.

    Args:
        context: The situation context; ``vip_rush_clean`` and ``vip_room_move``
            supply option titles/details when present.

    Returns:
        Two ranked options (rush-clean, room-move) at distinct ranks.
    """
    rush = context.vip_rush_clean or {
        "title": "Rush-clean assigned room",
        "detail": "Dispatch housekeeping to prioritize the assigned room now.",
    }
    move = context.vip_room_move or {
        "title": "Move guest to a ready room",
        "detail": "Reassign the guest to a comparable room that is already Ready.",
    }
    return [
        RankedOption(
            label="A",
            rank=1,
            title=str(rush["title"]),
            detail=str(rush["detail"]),
            recommended=True,
        ),
        RankedOption(
            label="B",
            rank=2,
            title=str(move["title"]),
            detail=str(move["detail"]),
            recommended=False,
        ),
    ]


# ---------------------------------------------------------------------------
# UC-04: Complaint options
# ---------------------------------------------------------------------------


def _parse_complaint_candidate(
    raw: Mapping[str, Any],
) -> tuple[str, str, float, ReviewRisk, bool]:
    """Validate and unpack a single complaint option candidate.

    Args:
        raw: The candidate mapping (``title``, ``detail``, ``estimatedCost``,
            ``reviewRisk``, optional ``recommended``).

    Returns:
        A tuple of ``(title, detail, estimated_cost, review_risk, recommended)``.

    Raises:
        TriageFailure: If a required field is missing or mistyped.
    """
    title = raw.get("title")
    detail = raw.get("detail")
    cost = raw.get("estimatedCost")
    risk = raw.get("reviewRisk")
    recommended = raw.get("recommended", False)

    if not isinstance(title, str) or not title:
        raise TriageFailure(
            "Complaint option title required", reason="malformed_option"
        )
    if not isinstance(detail, str) or not detail:
        raise TriageFailure(
            "Complaint option detail required", reason="malformed_option"
        )
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        raise TriageFailure(
            "Complaint option estimatedCost must be numeric", reason="malformed_option"
        )
    try:
        review_risk = ReviewRisk(risk)
    except ValueError as exc:
        raise TriageFailure(
            f"Complaint option reviewRisk invalid: {risk!r}", reason="malformed_option"
        ) from exc
    return title, detail, float(cost), review_risk, bool(recommended)


def build_complaint_options(
    candidates: Sequence[Mapping[str, Any]],
) -> list[RankedOption]:
    """Build 3-5 well-formed complaint remedy options (Requirements 5.2, 5.4).

    Each returned option carries a numeric estimated cost and a discrete
    review-risk level (Low/Medium/High), and exactly one option is marked
    recommended. When fewer than 3 well-formed candidates are available a
    :class:`TriageFailure` is raised so the caller keeps the alert CRITICAL,
    records the option-generation failure, and notifies the GM that manual
    resolution is required (Requirement 5.4).

    Args:
        candidates: Raw remedy option candidates (typically from the model).

    Returns:
        Between 3 and 5 ranked options (Property 10), ordered as supplied, with
        contiguous ranks starting at 1 and exactly one recommended.

    Raises:
        TriageFailure: If fewer than 3 candidates are supplied or any candidate
            is malformed.
    """
    if len(candidates) < COMPLAINT_MIN_OPTIONS:
        raise TriageFailure(
            f"Complaint triage requires at least {COMPLAINT_MIN_OPTIONS} options, "
            f"got {len(candidates)}",
            reason="insufficient_options",
        )

    selected = list(candidates[:COMPLAINT_MAX_OPTIONS])
    parsed = [_parse_complaint_candidate(candidate) for candidate in selected]

    # Exactly one recommended: honor a single flagged candidate; otherwise
    # default to the first option.
    flagged = [index for index, item in enumerate(parsed) if item[4]]
    recommended_index = flagged[0] if len(flagged) == 1 else 0

    options: list[RankedOption] = []
    for index, (title, detail, cost, risk, _flag) in enumerate(parsed):
        options.append(
            RankedOption(
                label=_label_for(index),
                rank=index + 1,
                title=title,
                detail=detail,
                recommended=index == recommended_index,
                estimated_cost=cost,
                review_risk=risk,
            )
        )
    return options


# ---------------------------------------------------------------------------
# UC-03: OOO Cluster replacement options
# ---------------------------------------------------------------------------


def build_ooo_replacement_options(context: SituationContext) -> list[RankedOption]:
    """Build up to 5 matched, suitability-ordered replacement rooms (Req 7.2, 7.4).

    Keeps only candidate rooms that match the affected group block's room type
    and are available for the full overlapping date range, orders them from
    highest to lowest match suitability, and presents at most 5. When no
    candidate matches, zero options are returned (Requirement 7.4).

    Args:
        context: The situation context; ``required_room_type`` and
            ``replacement_candidates`` (each with ``roomId``, ``roomType``,
            ``availableForRange``, ``suitability``) drive the selection.

    Returns:
        Up to 5 ranked options, or an empty list when no room matches.
    """
    required_type = context.required_room_type
    matched = [
        room
        for room in context.replacement_candidates
        if room.get("roomType") == required_type and bool(room.get("availableForRange"))
    ]
    # Highest to lowest match suitability.
    matched.sort(key=lambda room: room.get("suitability", 0.0), reverse=True)
    top = matched[:OOO_MAX_REPLACEMENTS]

    options: list[RankedOption] = []
    for index, room in enumerate(top):
        room_id = str(room.get("roomId"))
        options.append(
            RankedOption(
                label=_label_for(index),
                rank=index + 1,
                title=f"Replacement room {room_id}",
                detail=(
                    f"Room {room_id} ({required_type}) is available for the full "
                    f"overlapping range; match suitability "
                    f"{room.get('suitability', 0.0)}."
                ),
                recommended=index == 0,
            )
        )
    if not options:
        logger.info(
            "OOO cluster has no available replacement rooms",
            extra={"propertyId": context.property_id, "roomType": required_type},
        )
    return options


def draft_group_notification(
    context: SituationContext, options: Sequence[RankedOption]
) -> str:
    """Draft a group notification message for an OOO cluster (Requirement 7.3).

    References the affected group block and the proposed replacement rooms.

    Args:
        context: The situation context (carries the group block descriptor).
        options: The replacement-room options produced for the alert.

    Returns:
        A drafted notification message.
    """
    block = context.group_block or {}
    block_id = block.get("blockId") or "your group block"
    if not options:
        return (
            f"Regarding {block_id}: some rooms are temporarily out of order and no "
            "in-type replacement is currently available; our team is arranging "
            "alternatives and will follow up shortly."
        )
    room_list = ", ".join(option.title for option in options)
    return (
        f"Regarding {block_id}: due to rooms being out of order we propose the "
        f"following replacement rooms: {room_list}. Please confirm and we will "
        "update the reservations."
    )


__all__ = [
    "COMPLAINT_MIN_OPTIONS",
    "COMPLAINT_MAX_OPTIONS",
    "OOO_MAX_REPLACEMENTS",
    "DEFAULT_COMPENSATION_COST",
    "build_walk_strategy",
    "build_vip_options",
    "build_complaint_options",
    "build_ooo_replacement_options",
    "draft_group_notification",
]
