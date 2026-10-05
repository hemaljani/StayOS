"""Alert correlation keys and cleared-condition detection (closed loop).

The closed loop resolves the *originating* alert when its triggering condition
clears -- either because the Action Executor wrote back (primary path) or
because some other source (a real PMS write, a manual fix, or the demo reset)
cleared it (safety-net path, design Decision 6). Correlation is by ``dedupeKey``
/ ``sourceEntityRef``, never by write authorship (the Rule Engine cannot tell a
write-back from a genuine change).

This module centralizes two pure concerns so the evaluators and the loop-guard
never drift:

    * **Dedupe-key builders.** The exact ``dedupeKey`` format for each alert
      type, used by the evaluators when an alert fires and by the loop-guard to
      locate the correlated alert when the condition clears. The deterministic
      ``alertId`` is derived from the dedupe key (see
      :func:`pulse.rule_engine.alert_factory.derive_alert_id`).
    * **Cleared-condition detection.** :func:`detect_cleared_dedupe_keys`
      inspects an operational change and returns the dedupe keys of correlated
      entities whose (resolvable) trigger condition is currently *false* while
      its inputs are present -- i.e. conditions that just cleared. INFO types are
      never auto-resolved and are excluded.

Date-range helpers shared with the OOO evaluator live here too so the overlap
logic exists in one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from pulse.common.models import AlertType, OperationalChange, RuleDefinition

# Room status that means "ready" (must match the evaluator/executor constant).
ROOM_STATUS_READY = "Ready"

# Default VIP arrival threshold (minutes) when a rule omits one.
DEFAULT_ARRIVAL_THRESHOLD_MIN = 60

# Default minimum out-of-order cluster size when a rule omits one.
DEFAULT_MIN_CLUSTER_SIZE = 3


# ---------------------------------------------------------------------------
# Dedupe-key builders (shared by evaluators and the loop-guard)
# ---------------------------------------------------------------------------


def walk_risk_dedupe_key(property_id: str, arrival_date: str) -> str:
    """Return the Walk Risk dedupe key for a property/arrival date.

    Args:
        property_id: The property identifier.
        arrival_date: The arrival date (``YYYY-MM-DD``).

    Returns:
        The dedupe key ``WALK_RISK#<propertyId>#<arrivalDate>``.
    """
    return f"{AlertType.WALK_RISK.value}#{property_id}#{arrival_date}"


def vip_room_dedupe_key(property_id: str, guest_id: str) -> str:
    """Return the VIP Room Not Ready dedupe key for a property/guest.

    Args:
        property_id: The property identifier.
        guest_id: The VIP guest identifier.

    Returns:
        The dedupe key ``VIP_ROOM_NOT_READY#<propertyId>#<guestId>``.
    """
    return f"{AlertType.VIP_ROOM_NOT_READY.value}#{property_id}#{guest_id}"


def complaint_dedupe_key(property_id: str, complaint_id: str) -> str:
    """Return the Complaint Escalation dedupe key for a property/complaint.

    Args:
        property_id: The property identifier.
        complaint_id: The complaint identifier.

    Returns:
        The dedupe key ``COMPLAINT_ESCALATION#<propertyId>#<complaintId>``.
    """
    return f"{AlertType.COMPLAINT_ESCALATION.value}#{property_id}#{complaint_id}"


def ooo_cluster_dedupe_key(property_id: str, block_id: str) -> str:
    """Return the OOO Cluster dedupe key for a property/group block.

    Args:
        property_id: The property identifier.
        block_id: The group-block identifier.

    Returns:
        The dedupe key ``OOO_CLUSTER#<propertyId>#<blockId>``.
    """
    return f"{AlertType.OOO_CLUSTER.value}#{property_id}#{block_id}"


def premium_cancellation_dedupe_key(property_id: str, reservation_id: str) -> str:
    """Return the Premium Cancellation dedupe key for a property/reservation.

    Args:
        property_id: The property identifier.
        reservation_id: The reservation identifier.

    Returns:
        The dedupe key ``PREMIUM_CANCELLATION#<propertyId>#<reservationId>``.
    """
    return f"{AlertType.PREMIUM_CANCELLATION.value}#{property_id}#{reservation_id}"


def vip_checkin_dedupe_key(property_id: str, guest_id: str, stay_id: str) -> str:
    """Return the VIP Check-In dedupe key for a property/guest/stay.

    Args:
        property_id: The property identifier.
        guest_id: The guest identifier.
        stay_id: The stay identifier.

    Returns:
        The dedupe key ``VIP_CHECKIN#<propertyId>#<guestId>#<stayId>``.
    """
    return f"{AlertType.VIP_CHECKIN.value}#{property_id}#{guest_id}#{stay_id}"


# ---------------------------------------------------------------------------
# Date-range helpers (shared with the OOO evaluator)
# ---------------------------------------------------------------------------


def parse_iso_date(value: Any) -> Optional[date]:
    """Parse an ISO 8601 date (``YYYY-MM-DD``), returning ``None`` on failure.

    Args:
        value: The raw date string.

    Returns:
        The parsed date, or ``None`` when it cannot be parsed.
    """
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def ranges_overlap_nights(
    start_a: date, end_a: date, start_b: date, end_b: date
) -> bool:
    """Return whether two ``[start, end)`` ranges overlap by >= 1 night.

    Args:
        start_a: Start of the first range.
        end_a: End of the first range (exclusive).
        start_b: Start of the second range.
        end_b: End of the second range (exclusive).

    Returns:
        ``True`` if the ranges share at least one night.
    """
    return start_a < end_b and start_b < end_a


# ---------------------------------------------------------------------------
# Cleared-condition detection (safety-net auto-resolution)
# ---------------------------------------------------------------------------


def _coerce_int(value: Any) -> Optional[int]:
    """Coerce a numeric value to ``int``, returning ``None`` when impossible.

    Args:
        value: The raw value (``int``/``Decimal``/str).

    Returns:
        The integer value, or ``None`` when the value is absent/non-numeric or a
        boolean (which is not a valid count).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, Decimal)):
            return int(value)
        return int(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None


def _image(change: OperationalChange) -> Optional[Mapping[str, Any]]:
    """Return the change's new image, falling back to the old image.

    Args:
        change: The normalized operational change.

    Returns:
        The most relevant item image, or ``None`` when neither is present.
    """
    return change.new_image if change.new_image is not None else change.old_image


def _walk_cleared(image: Mapping[str, Any], property_id: str) -> list[str]:
    """Return the Walk Risk dedupe key when its condition is cleared.

    Args:
        image: The operational item image.
        property_id: The change's property id.

    Returns:
        ``[dedupe_key]`` when confirmed <= available (cleared), else ``[]``.
    """
    arrival_date = image.get("arrivalDate")
    confirmed = _coerce_int(image.get("confirmedReservations"))
    available = _coerce_int(image.get("availableRooms"))
    if arrival_date is None or confirmed is None or available is None:
        return []
    if confirmed <= available:
        return [walk_risk_dedupe_key(property_id, str(arrival_date))]
    return []


def _vip_room_cleared(
    image: Mapping[str, Any], property_id: str, rule: RuleDefinition
) -> list[str]:
    """Return the VIP Room dedupe key when its condition is cleared.

    Args:
        image: The operational item image.
        property_id: The change's property id.
        rule: The VIP Room rule (for the arrival threshold parameter).

    Returns:
        ``[dedupe_key]`` when the room is ready or the ETA is beyond threshold,
        else ``[]`` (also ``[]`` when identifying/condition inputs are absent).
    """
    guest_id = image.get("guestId")
    eta = _coerce_int(image.get("etaMinutes"))
    room_status = image.get("assignedRoomStatus")
    if guest_id is None or eta is None or room_status is None:
        return []
    threshold = int(
        rule.parameters.get("arrivalThresholdMin", DEFAULT_ARRIVAL_THRESHOLD_MIN)
    )
    condition_holds = eta <= threshold and room_status != ROOM_STATUS_READY
    if not condition_holds:
        return [vip_room_dedupe_key(property_id, str(guest_id))]
    return []


def _complaint_cleared(image: Mapping[str, Any], property_id: str) -> list[str]:
    """Return the Complaint dedupe key when its escalation flag is cleared.

    Args:
        image: The operational item image.
        property_id: The change's property id.

    Returns:
        ``[dedupe_key]`` when the escalation flag is falsey, else ``[]``.
    """
    complaint_id = image.get("complaintId")
    if complaint_id is None:
        return []
    if not bool(image.get("complaintEscalationFlag")):
        return [complaint_dedupe_key(property_id, str(complaint_id))]
    return []


def _ooo_cleared(
    image: Mapping[str, Any], property_id: str, rule: RuleDefinition
) -> list[str]:
    """Return OOO dedupe keys for group blocks whose cluster has cleared.

    Args:
        image: The property OOO/blocks snapshot image.
        property_id: The change's property id.
        rule: The OOO rule (for the minimum cluster-size parameter).

    Returns:
        A dedupe key for each group block that no longer has an overlapping
        cluster of the required size (possibly empty).
    """
    group_blocks = image.get("groupBlocks")
    if group_blocks is None:
        return []
    min_cluster = int(rule.parameters.get("minClusterSize", DEFAULT_MIN_CLUSTER_SIZE))
    ooo_rooms = image.get("oooRooms") or []
    cleared: list[str] = []
    for block in group_blocks:
        block_start = parse_iso_date(block.get("startDate"))
        block_end = parse_iso_date(block.get("endDate"))
        if block_start is None or block_end is None:
            continue
        overlapping = 0
        for room in ooo_rooms:
            room_start = parse_iso_date(room.get("startDate"))
            room_end = parse_iso_date(room.get("endDate"))
            if room_start is None or room_end is None:
                continue
            if ranges_overlap_nights(room_start, room_end, block_start, block_end):
                overlapping += 1
        if overlapping < min_cluster:
            block_id = str(block.get("blockId", "unknown-block"))
            cleared.append(ooo_cluster_dedupe_key(property_id, block_id))
    return cleared


# Resolvable (executor-handled) types whose cleared condition auto-resolves the
# correlated alert. INFO types are informational and never auto-resolved.
_CLEARED_DETECTORS = {
    AlertType.WALK_RISK: lambda image, pid, rule: _walk_cleared(image, pid),
    AlertType.VIP_ROOM_NOT_READY: _vip_room_cleared,
    AlertType.COMPLAINT_ESCALATION: lambda image, pid, rule: _complaint_cleared(
        image, pid
    ),
    AlertType.OOO_CLUSTER: _ooo_cleared,
}


def detect_cleared_dedupe_keys(
    change: OperationalChange, rule: RuleDefinition
) -> list[str]:
    """Return dedupe keys of correlated entities whose condition just cleared.

    For a resolvable rule type, inspects the change image and returns the dedupe
    key(s) of the correlated entities whose trigger condition is currently
    *false* while its inputs are present. The loop-guard uses these to resolve
    still-open correlated alerts (design Decision 6, Property 27). Returns an
    empty list for non-resolvable types, irrelevant changes (identifying inputs
    absent), or conditions that are still firing.

    Args:
        change: The normalized operational change.
        rule: A single enabled rule definition for the change's property.

    Returns:
        The correlated dedupe keys whose condition has cleared (possibly empty).
    """
    detector = _CLEARED_DETECTORS.get(rule.rule_type)
    if detector is None:
        return []
    image = _image(change)
    if image is None or not change.property_id:
        return []
    return detector(image, change.property_id, rule)


__all__ = [
    "ROOM_STATUS_READY",
    "DEFAULT_ARRIVAL_THRESHOLD_MIN",
    "DEFAULT_MIN_CLUSTER_SIZE",
    "walk_risk_dedupe_key",
    "vip_room_dedupe_key",
    "complaint_dedupe_key",
    "ooo_cluster_dedupe_key",
    "premium_cancellation_dedupe_key",
    "vip_checkin_dedupe_key",
    "parse_iso_date",
    "ranges_overlap_nights",
    "detect_cleared_dedupe_keys",
]
