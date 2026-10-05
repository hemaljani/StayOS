"""Per-alert-type fact gathering: Gateway tool results -> SituationContext.

The Triage Agent gathers the facts each alert type needs by calling the shared
Gateway's read-only tools, then assembles a :class:`pulse.triage.context.
SituationContext`. The downstream ``generate_triage_brief`` renders the
per-type prompt (narrative from the model) and applies the deterministic
``pulse.triage.specializations`` so the structural guarantees (Property 18, the
Walk_Strategy cap/threshold, the OOO type-match, the VIP option pair) hold
regardless of what the model returns.

Tier-name reconciliation (IMPORTANT):
    The LUMI dataset labels loyalty tiers ``AMBASSADOR`` / ``TITANIUM`` /
    ``PLATINUM`` (ascending elite-ness for walk eligibility:
    ``PLATINUM`` < ``TITANIUM`` < ``AMBASSADOR``), which differ from the PULSE
    glossary vocabulary that ``pulse.triage.context.loyalty_rank`` understands
    (``Member`` < ``Silver`` < ``Gold`` < ``Platinum`` < ``Ambassador``). The
    ``get_walkable_guests`` Gateway tool already applies the *authoritative*
    walkable selection server-side (at/below the protection threshold, ordered
    least-elite first, capped at the shortfall). To keep the deterministic
    re-filter inside ``build_walk_strategy`` a faithful **no-op** that trusts the
    tool rather than accidentally re-excluding guests, this module maps each LUMI
    tier token into the PULSE vocabulary preserving the relative ordering, and
    maps the requested protection tier through the same table. See
    :data:`LUMI_TO_PULSE_LOYALTY_TIER`.

Every tool call passes ``propertyId`` so the Gateway scopes the result
server-side (property scope comes from the invocation context, never the model).
All builders take a :data:`~gateway.ToolCaller` seam, so they are unit-testable
with an in-memory fake and never open a network connection.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from gateway import ToolCaller

from pulse.common.errors import TriageFailure
from pulse.common.logging import get_logger
from pulse.common.models import AlertType
from pulse.triage.context import SituationContext

logger = get_logger("pulse-triage-agent")

# Reconcile LUMI dataset loyalty tiers into the PULSE vocabulary understood by
# pulse.triage.context.loyalty_rank, preserving the walk-eligibility ordering
# PLATINUM < TITANIUM < AMBASSADOR -> Gold(2) < Platinum(3) < Ambassador(4).
LUMI_TO_PULSE_LOYALTY_TIER: dict[str, str] = {
    "PLATINUM": "Gold",
    "TITANIUM": "Platinum",
    "AMBASSADOR": "Ambassador",
}

# Default loyalty protection tier for Walk Risk when the invocation carries none
# (matches the get_walkable_guests tool default: least-elite only).
DEFAULT_LOYALTY_PROTECTION_TIER = "PLATINUM"

# The PULSE tier the SituationContext advertises as the protection threshold.
# Set to the most-elite PULSE tier ("Ambassador", rank 4) so build_walk_strategy
# trusts the tool's already-applied threshold: every mapped guest tier (ranks
# 2-4) satisfies rank <= 4, so the deterministic re-filter never drops a guest
# the tool deemed walkable; the shortfall cap still re-bounds the count.
_TRUSTED_PROTECTION_TIER = "Ambassador"


def _as_list(value: Any) -> list[Any]:
    """Return a list from a tool result that may wrap items under a key.

    Gateway tools may return either a bare JSON array or an object wrapping the
    array (e.g. ``{"guests": [...]}`` / ``{"rooms": [...]}`` / ``{"items": [...]}``).
    This normalizes both shapes to a plain list.

    Args:
        value: The decoded tool result.

    Returns:
        The list of items, or an empty list when none can be found.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("guests", "rooms", "candidates", "properties", "items", "results"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
    return []


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a tool-result value to a non-negative int, defaulting on failure.

    Args:
        value: The raw value (number or numeric string).
        default: The fallback when ``value`` is missing or non-numeric.

    Returns:
        The coerced integer (never negative), or ``default``.
    """
    try:
        if isinstance(value, bool) or value is None:
            return default
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-None value among candidate keys.

    Tool payloads are camelCase but field spellings can vary; this tolerates a
    few reasonable aliases without hardcoding one exact schema.

    Args:
        mapping: The source mapping.
        *keys: Candidate keys in priority order.

    Returns:
        The first matching value, or ``None`` when none is present.
    """
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _map_guest_tier(guest: dict[str, Any]) -> Optional[str]:
    """Map a guest's LUMI loyalty tier into the PULSE vocabulary.

    Args:
        guest: A walkable-guest mapping from ``get_walkable_guests``.

    Returns:
        The PULSE tier token, or the original value when it is already a PULSE
        tier or unknown (``loyalty_rank`` then applies its default).
    """
    raw = _first_present(guest, "loyaltyTier", "tier")
    if not isinstance(raw, str):
        return None
    return LUMI_TO_PULSE_LOYALTY_TIER.get(raw.upper(), raw)


def _normalize_walkable_guests(raw_guests: Sequence[Any]) -> list[dict[str, Any]]:
    """Normalize walkable-guest tool results into SituationContext guest dicts.

    Args:
        raw_guests: The guest entries from ``get_walkable_guests``.

    Returns:
        Guest dicts with ``guestId``, ``reservationId``, and a PULSE-mapped
        ``loyaltyTier``.
    """
    normalized: list[dict[str, Any]] = []
    for guest in raw_guests:
        if not isinstance(guest, dict):
            continue
        normalized.append(
            {
                "guestId": _first_present(guest, "guestId", "guest_id", "id"),
                "reservationId": _first_present(
                    guest, "reservationId", "reservation_id"
                ),
                "loyaltyTier": _map_guest_tier(guest),
            }
        )
    return normalized


def _build_sister_lookup(raw_properties: Sequence[Any]) -> Any:
    """Build a sister-property lookup seam from availability tool results.

    ``build_walk_strategy`` calls the lookup with the ``(start, end)`` stay
    dates and expects the id of a sister property with availability, or ``None``.
    The availability was already fetched (one Gateway call), so the closure just
    returns the first property reporting available rooms.

    Args:
        raw_properties: The entries from ``get_sister_property_availability``.

    Returns:
        A ``Callable[[tuple[str, str]], Optional[str]]`` lookup seam.
    """
    available_ids: list[str] = []
    for prop in raw_properties:
        if not isinstance(prop, dict):
            continue
        count = _as_int(
            _first_present(prop, "availableRooms", "available", "roomsAvailable")
        )
        prop_id = _first_present(prop, "propertyId", "property_id", "id")
        if count > 0 and isinstance(prop_id, str):
            available_ids.append(prop_id)

    def _lookup(_stay_dates: tuple[str, str]) -> Optional[str]:
        return available_ids[0] if available_ids else None

    return _lookup


# ---------------------------------------------------------------------------
# Per-alert-type builders
# ---------------------------------------------------------------------------


def build_walk_risk_context(
    property_id: str,
    tool_caller: ToolCaller,
    *,
    loyalty_protection_tier: str = DEFAULT_LOYALTY_PROTECTION_TIER,
) -> SituationContext:
    """Gather Walk Risk facts and assemble the SituationContext (UC-01).

    Tools -> fields:
        * ``get_occupancy`` -> the room shortfall (arrivals minus available
          rooms) and the oversold date (stay dates).
        * ``get_walkable_guests(shortfall, protectionTier, arrivalDate)`` ->
          ``confirmed_guests`` (tiers reconciled to the PULSE vocabulary).
        * ``get_sister_property_availability(startDate, endDate)`` ->
          ``sister_property_lookup`` (first property with availability).

    Args:
        property_id: The alert's property (server-side scope for every tool).
        tool_caller: The Gateway tool-call seam.
        loyalty_protection_tier: The LUMI protection tier (guests at/below are
            walkable); defaults to ``PLATINUM``.

    Returns:
        A Walk Risk :class:`SituationContext`.
    """
    occupancy = tool_caller("get_occupancy", {"propertyId": property_id})
    occ = occupancy if isinstance(occupancy, dict) else {}
    # Walk risk is confirmed demand vs. sellable rooms. Use confirmedReservations
    # (the firm booked count) minus availableRooms, NOT arrivalsTotal (a
    # different, smaller same-day-arrival figure that made the shortfall read 0
    # and left the deterministic strategy empty). Fall back to arrivals only if
    # confirmedReservations is absent.
    confirmed = _as_int(
        _first_present(occ, "confirmedReservations", "confirmed", "confirmedCount")
    )
    arrivals = _as_int(_first_present(occ, "arrivals", "totalArrivals"))
    available_rooms = _as_int(
        _first_present(occ, "availableRooms", "available", "roomsAvailable")
    )
    demand = confirmed if confirmed > 0 else arrivals
    shortfall = max(demand - available_rooms, 0)
    arrival_date = _first_present(occ, "date", "arrivalDate", "businessDate")
    stay_dates = (
        (str(arrival_date), str(arrival_date)) if arrival_date else None
    )

    walkable_raw = tool_caller(
        "get_walkable_guests",
        {
            "propertyId": property_id,
            "shortfall": shortfall,
            "loyaltyProtectionTier": loyalty_protection_tier,
            **({"arrivalDate": str(arrival_date)} if arrival_date else {}),
        },
    )
    confirmed_guests = _normalize_walkable_guests(_as_list(walkable_raw))

    # NOTE: Walk Risk no longer recommends relocating to a same-brand "sister"
    # property. The pilot estate is one hotel per city, so a same-brand sister
    # is in a different city/country - walking a guest there is not a realistic
    # GM action. The strategy is now grounded in in-house actions + a generic
    # external partner-overflow (see build_walk_strategy + the walk_risk prompt),
    # so we deliberately do NOT call get_sister_property_availability here and
    # pass no sister lookup. (The tool still exists for other consumers.)

    logger.info(
        "Assembled Walk Risk situation context",
        extra={
            "property_id": property_id,
            "shortfall": shortfall,
            "walkableGuestCount": len(confirmed_guests),
        },
    )
    return SituationContext(
        property_id=property_id,
        confirmed_guests=confirmed_guests,
        room_shortfall=shortfall,
        # Advertise the most-elite PULSE tier so the deterministic re-filter
        # trusts the tool's authoritative threshold (see module docstring).
        loyalty_protection_tier=_TRUSTED_PROTECTION_TIER,
        stay_dates=stay_dates,
        # No sister-property lookup: in-house/partner-overflow framing (Option B).
        sister_property_lookup=None,
    )


def build_vip_room_not_ready_context(
    property_id: str, tool_caller: ToolCaller
) -> SituationContext:
    """Gather VIP Room Not Ready facts and assemble the context (UC-02).

    Tools -> fields:
        * ``get_room_move_candidates`` -> a concrete room-move option (referenced
          room id) when a ready room is available.
        * ``get_room_status`` / ``get_vip_guests`` -> narrative context.

    ``build_vip_options`` always guarantees a rush-clean + room-move option pair
    at distinct ranks (Requirement 4.2); this context only enriches the
    room-move detail when a candidate exists.

    Args:
        property_id: The alert's property (server-side scope for every tool).
        tool_caller: The Gateway tool-call seam.

    Returns:
        A VIP Room Not Ready :class:`SituationContext`.
    """
    move_raw = tool_caller("get_room_move_candidates", {"propertyId": property_id})
    move_candidates = _as_list(move_raw)
    # Read room status for narrative context (unblocked-room detection lives in
    # the model prompt; the option pair is guaranteed by the specialization).
    tool_caller("get_room_status", {"propertyId": property_id})

    vip_room_move: Optional[dict[str, Any]] = None
    if move_candidates:
        first = move_candidates[0] if isinstance(move_candidates[0], dict) else {}
        room_id = _first_present(first, "roomId", "roomNumber", "id")
        room_type = _first_present(first, "roomType", "type")
        detail = "Reassign the guest to a comparable room that is already Ready."
        if room_id:
            detail = (
                f"Reassign the guest to ready room {room_id}"
                f"{f' ({room_type})' if room_type else ''}, available now."
            )
        vip_room_move = {"title": "Move guest to a ready room", "detail": detail}

    logger.info(
        "Assembled VIP Room Not Ready situation context",
        extra={
            "property_id": property_id,
            "moveCandidateCount": len(move_candidates),
        },
    )
    return SituationContext(
        property_id=property_id,
        vip_room_move=vip_room_move,
    )


def build_ooo_cluster_context(
    property_id: str, tool_caller: ToolCaller, block_id: Optional[str] = None
) -> SituationContext:
    """Gather OOO Cluster facts and assemble the context (UC-03).

    Tools -> fields:
        * ``get_room_status`` -> the out-of-order rooms and the affected
          ``required_room_type`` (the most common OOO room type).
        * ``get_room_move_candidates(roomType)`` -> ``replacement_candidates``
          (mapped to ``roomId`` / ``roomType`` / ``availableForRange`` /
          ``suitability``).

    ``build_ooo_replacement_options`` keeps only type-matched, available rooms,
    ordered by suitability, up to 5 (zero when none, Requirement 7.4).

    Args:
        property_id: The alert's property (server-side scope for every tool).
        tool_caller: The Gateway tool-call seam.
        block_id: The affected group-block identifier from the alert (parsed
            from the OOO alert's dedupe key). When absent, a neutral descriptor
            is used so no prompt-template placeholder leaks into the brief.

    Returns:
        An OOO Cluster :class:`SituationContext`.
    """
    room_status = tool_caller("get_room_status", {"propertyId": property_id})
    ooo_rooms = _as_list(room_status)

    # Determine the affected room type: the most frequent room type among OOO
    # rooms. Fall back to None (then no replacement matches, per Requirement 7.4).
    type_counts: dict[str, int] = {}
    for room in ooo_rooms:
        if not isinstance(room, dict):
            continue
        room_type = _first_present(room, "roomType", "type")
        if isinstance(room_type, str):
            type_counts[room_type] = type_counts.get(room_type, 0) + 1
    required_room_type: Optional[str] = (
        max(type_counts, key=lambda key: type_counts[key]) if type_counts else None
    )

    move_args: dict[str, Any] = {"propertyId": property_id}
    if required_room_type:
        move_args["roomType"] = required_room_type
    move_raw = tool_caller("get_room_move_candidates", move_args)

    replacement_candidates: list[dict[str, Any]] = []
    for room in _as_list(move_raw):
        if not isinstance(room, dict):
            continue
        replacement_candidates.append(
            {
                "roomId": _first_present(room, "roomId", "roomNumber", "id"),
                "roomType": _first_present(room, "roomType", "type"),
                # A room returned by get_room_move_candidates is ready/available,
                # so it is available for the overlapping range.
                "availableForRange": True,
                "suitability": _first_present(room, "suitability") or 1.0,
            }
        )

    logger.info(
        "Assembled OOO Cluster situation context",
        extra={
            "property_id": property_id,
            "requiredRoomType": required_room_type,
            "replacementCandidateCount": len(replacement_candidates),
        },
    )
    # Use the real block id from the alert when available; otherwise a neutral
    # descriptor (never the prompt-template placeholder "the affected group
    # block", which previously leaked verbatim into GM-facing briefs).
    group_block = {"blockId": block_id} if block_id else {"blockId": "unspecified"}
    return SituationContext(
        property_id=property_id,
        required_room_type=required_room_type,
        replacement_candidates=replacement_candidates,
        group_block=group_block,
    )


def build_complaint_context(
    property_id: str, tool_caller: ToolCaller
) -> SituationContext:
    """Gather Guest Complaint Escalation context (UC-04).

    The 3-5 remedy options (each with an estimated cost and review-risk level)
    are produced by the model from the complaint prompt and enforced by
    ``build_complaint_options``; there is no ops tool for remedies. This context
    only supplies the property and currency plus light operational context
    (occupancy) for the narrative.

    Args:
        property_id: The alert's property (server-side scope for every tool).
        tool_caller: The Gateway tool-call seam.

    Returns:
        A Complaint Escalation :class:`SituationContext`.
    """
    tool_caller("get_occupancy", {"propertyId": property_id})
    logger.info(
        "Assembled Complaint situation context", extra={"property_id": property_id}
    )
    return SituationContext(property_id=property_id, currency="USD")


def build_premium_cancellation_context(
    property_id: str, tool_caller: ToolCaller
) -> SituationContext:
    """Gather Premium Cancellation facts and assemble the context (UC-05, INFO).

    A premium reservation was cancelled, freeing a high-value room to resell.
    The advisory brief recommends a re-list / rate action, so the useful facts
    are revenue KPIs (to justify a rate move) and occupancy (to decide re-list
    vs. discount). There is no executable write-back for INFO alerts; the brief
    is purely advisory.

    Tools -> facts (stored under ``extra`` for prompt rendering):
        * ``get_revenue`` -> adr, revpar, currency, vsLastWeek/vsBudget/vsYOY.
        * ``get_occupancy`` -> occupancyPct, availableRooms, confirmed/arrivals.

    Args:
        property_id: The alert's property (server-side scope for every tool).
        tool_caller: The Gateway tool-call seam.

    Returns:
        A Premium Cancellation :class:`SituationContext`.
    """
    revenue = tool_caller("get_revenue", {"propertyId": property_id})
    occupancy = tool_caller("get_occupancy", {"propertyId": property_id})

    # Carry the property's currency onto the context when the tool reported one
    # (falls back to the SituationContext default of "USD").
    currency = "USD"
    if isinstance(revenue, dict):
        currency = str(_first_present(revenue, "currency") or "USD")

    logger.info(
        "Assembled Premium Cancellation situation context",
        extra={"property_id": property_id},
    )
    return SituationContext(
        property_id=property_id,
        currency=currency,
        extra={
            "revenue": revenue if isinstance(revenue, dict) else {},
            "occupancy": occupancy if isinstance(occupancy, dict) else {},
        },
    )


def build_vip_checkin_context(
    property_id: str, tool_caller: ToolCaller
) -> SituationContext:
    """Gather VIP Check-In facts and assemble the context (UC-06, INFO).

    A VIP guest just checked in. The advisory brief recommends a personalized
    welcome and any tasteful upsell, so the useful facts are the VIP guest
    profile (loyalty tier, room type, preferences / special requests) and
    occupancy (whether higher-category rooms are available for an upgrade).
    There is no executable write-back for INFO alerts; the brief is advisory.

    Tools -> facts (stored under ``extra`` for prompt rendering):
        * ``get_vip_guests`` -> the VIP arrivals list (loyaltyTier, roomType,
          specialRequests); PII (sensitiveNotes) is already stripped by the tool.
        * ``get_occupancy`` -> occupancyPct, availableRooms (upsell headroom).

    Args:
        property_id: The alert's property (server-side scope for every tool).
        tool_caller: The Gateway tool-call seam.

    Returns:
        A VIP Check-In :class:`SituationContext`.
    """
    vip_raw = tool_caller("get_vip_guests", {"propertyId": property_id})
    occupancy = tool_caller("get_occupancy", {"propertyId": property_id})

    # The VIP tool returns a guest list; surface it as-is for the model to match
    # the checked-in guest and personalize. The list is already PII-stripped.
    vip_guests = _as_list(vip_raw)

    logger.info(
        "Assembled VIP Check-In situation context",
        extra={"property_id": property_id, "vipGuestCount": len(vip_guests)},
    )
    return SituationContext(
        property_id=property_id,
        extra={
            "vipGuests": vip_guests,
            "occupancy": occupancy if isinstance(occupancy, dict) else {},
        },
    )


# Dispatch table: alert type -> builder. The two INFO types (premium
# cancellation, VIP check-in) build advisory-opportunity contexts.
_BUILDERS = {
    AlertType.WALK_RISK: build_walk_risk_context,
    AlertType.VIP_ROOM_NOT_READY: build_vip_room_not_ready_context,
    AlertType.OOO_CLUSTER: build_ooo_cluster_context,
    AlertType.COMPLAINT_ESCALATION: build_complaint_context,
    AlertType.PREMIUM_CANCELLATION: build_premium_cancellation_context,
    AlertType.VIP_CHECKIN: build_vip_checkin_context,
}


def build_situation_context(
    alert_type: AlertType,
    property_id: str,
    tool_caller: ToolCaller,
    block_id: Optional[str] = None,
) -> SituationContext:
    """Assemble the SituationContext for an alert type via the Gateway tools.

    Args:
        alert_type: The triage-eligible alert type.
        property_id: The alert's property (server-side tool scope).
        tool_caller: The Gateway tool-call seam.
        block_id: OOO Cluster only - the affected group-block id from the alert;
            ignored by other alert types.

    Returns:
        The assembled :class:`SituationContext`.

    Raises:
        TriageFailure: If the alert type has no registered builder.
    """
    builder = _BUILDERS.get(alert_type)
    if builder is None:
        raise TriageFailure(
            f"Alert type {alert_type.value} is not triage-eligible",
            reason="unsupported_type",
        )
    if alert_type is AlertType.OOO_CLUSTER:
        return build_ooo_cluster_context(property_id, tool_caller, block_id=block_id)
    return builder(property_id, tool_caller)


__all__ = [
    "LUMI_TO_PULSE_LOYALTY_TIER",
    "DEFAULT_LOYALTY_PROTECTION_TIER",
    "build_walk_risk_context",
    "build_vip_room_not_ready_context",
    "build_ooo_cluster_context",
    "build_complaint_context",
    "build_premium_cancellation_context",
    "build_vip_checkin_context",
    "build_situation_context",
]
