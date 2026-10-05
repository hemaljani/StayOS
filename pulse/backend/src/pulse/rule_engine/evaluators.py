"""Per-alert-type rule evaluators (UC-01 through UC-06).

Each function here is a **pure** evaluator: given a normalized
:class:`OperationalChange` and a single enabled :class:`RuleDefinition`, it
returns an :class:`AlertDraft` when the rule's trigger fires, or ``None`` when
it does not. Evaluators contain no Lambda, DynamoDB, or Bedrock dependency, so
they are unit-testable in isolation (design Component 1).

Missing-data handling differs by requirement:
    * Most evaluators raise :class:`RuleEvaluationError` when a required source
      field is absent, so the handler records a CloudWatch evaluation error and
      continues the batch (Requirement 1.6).
    * VIP Room Not Ready is the exception: missing ETA or room status still
      produces a CRITICAL alert flagged ``incompleteInputData`` (Requirement
      4.4).
    * An invalid VIP check-in record (missing identifying fields) is suppressed
      and logged, not raised (Requirement 9.5).

Operational item attribute names are camelCase (NAMING-05). The expected shape
of each source item is documented on its evaluator; these mirror the mock
operational schema the Demo Scenario Simulator writes (design Component 7).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import InvalidOperation
from typing import Any, Optional

from pulse.common.errors import RuleEvaluationError
from pulse.common.logging import get_logger
from pulse.common.models import (
    AlertDraft,
    AlertType,
    OperationalChange,
    RuleDefinition,
    SourceEntityRef,
)
from pulse.rule_engine.alert_factory import build_alert_draft, utc_now_iso
from pulse.rule_engine.correlation import (
    complaint_dedupe_key,
    ooo_cluster_dedupe_key,
    premium_cancellation_dedupe_key,
    ranges_overlap_nights,
    vip_checkin_dedupe_key,
    vip_room_dedupe_key,
    walk_risk_dedupe_key,
)
from pulse.rule_engine.rules_repository import evaluate_condition, register_evaluator

logger = get_logger("pulse-rule-evaluator")

# The status value that means a room is ready for a guest. Any other value
# (Dirty, Cleaning In Progress, Inspection Pending, Out Of Service, ...) is
# treated as not-ready (Requirement 4.1).
ROOM_STATUS_READY = "Ready"

# Default VIP arrival threshold in minutes when the rule does not configure one
# (Requirement 4.1: 15-240, default 60).
DEFAULT_ARRIVAL_THRESHOLD_MIN = 60

# Minimum out-of-order room count that constitutes a "cluster" (Requirement
# 7.1). Configurable per rule via parameters["minClusterSize"].
DEFAULT_MIN_CLUSTER_SIZE = 3


# ---------------------------------------------------------------------------
# Shared extraction helpers
# ---------------------------------------------------------------------------


def _image(change: OperationalChange) -> Mapping[str, Any]:
    """Return the change's new image, or the old image for a REMOVE event.

    Args:
        change: The normalized operational change.

    Returns:
        The most relevant item image (new image, falling back to old image for
        deletions).

    Raises:
        RuleEvaluationError: If neither image is present.
    """
    image = change.new_image if change.new_image is not None else change.old_image
    if image is None:
        raise RuleEvaluationError(
            f"Stream change on {change.table!r} carries no item image",
            detail="missing-image",
        )
    return image


def _require(image: Mapping[str, Any], key: str, rule_type: str) -> Any:
    """Return a required attribute or raise an un-evaluable error.

    Args:
        image: The item image to read from.
        key: The required camelCase attribute name.
        rule_type: The rule type, for the error context.

    Returns:
        The attribute value.

    Raises:
        RuleEvaluationError: If the attribute is absent or ``None`` (Requirement
            1.6: required source data missing).
    """
    if key not in image or image[key] is None:
        raise RuleEvaluationError(
            f"Required field {key!r} missing for {rule_type} evaluation",
            rule_type=rule_type,
            detail=key,
        )
    return image[key]


def _as_int(value: Any) -> int:
    """Coerce a DynamoDB numeric to ``int``.

    Args:
        value: The raw numeric value (``int`` or ``Decimal``).

    Returns:
        The integer value.

    Raises:
        RuleEvaluationError: If the value cannot be interpreted as an integer.
    """
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not a numeric count")
        return int(value)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise RuleEvaluationError(
            f"Expected an integer count, got {value!r}", detail="non-integer"
        ) from exc


def _source_ref(
    change: OperationalChange, property_id: str, entity_key: str, rule_type: str
) -> SourceEntityRef:
    """Build a :class:`SourceEntityRef` correlating an alert to its source.

    Args:
        change: The originating operational change (for the table name).
        property_id: The affected property.
        entity_key: The source entity key (arrival date, guest id, etc.).
        rule_type: The rule type that fired.

    Returns:
        A source-entity correlation pointer.
    """
    return {
        "table": change.table,
        "propertyId": property_id,
        "entityKey": entity_key,
        "ruleType": rule_type,
    }


def _parse_iso_date(value: Any) -> date:
    """Parse an ISO 8601 date string (``YYYY-MM-DD``) into a ``date``.

    Args:
        value: The raw date string.

    Returns:
        The parsed date.

    Raises:
        RuleEvaluationError: If the value is not a parseable ISO date.
    """
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError) as exc:
        raise RuleEvaluationError(f"Unparseable date {value!r}", detail="date") from exc


# ---------------------------------------------------------------------------
# UC-01: Walk Risk (CRITICAL)
# ---------------------------------------------------------------------------


@register_evaluator(AlertType.WALK_RISK)
def evaluate_walk_risk(
    change: OperationalChange, rule: RuleDefinition
) -> Optional[AlertDraft]:
    """Evaluate the Walk Risk trigger (UC-01, Requirements 3.1, 3.2).

    Fires when the confirmed reservation count exceeds the available room count
    for a property. The reported shortfall equals confirmed minus available.

    Expected ``new_image`` attributes (``stayos-reservations`` aggregate item):
        ``propertyId``, ``arrivalDate``, ``confirmedReservations``,
        ``availableRooms``.

    Args:
        change: The operational change (a reservations aggregate update).
        rule: The Walk Risk rule definition.

    Returns:
        A CRITICAL :class:`AlertDraft` when confirmed exceeds available, else
        ``None``.

    Raises:
        RuleEvaluationError: If a required count or key is missing.
    """
    image = _image(change)
    rule_type = AlertType.WALK_RISK.value
    property_id = _require(image, "propertyId", rule_type)
    arrival_date = _require(image, "arrivalDate", rule_type)
    confirmed = _as_int(_require(image, "confirmedReservations", rule_type))
    available = _as_int(_require(image, "availableRooms", rule_type))

    # Evaluate through the declarative condition model so the admin-editable
    # operator/operands drive the comparison (no eval).
    context = {
        "reservations.confirmed": confirmed,
        "rooms.available": available,
    }
    if not evaluate_condition(rule.trigger_condition, context):
        return None

    shortfall = confirmed - available
    created_at = utc_now_iso()
    detail = (
        f"Property {property_id}: {confirmed} confirmed reservations vs "
        f"{available} available rooms; shortfall {shortfall}. "
        f"Created {created_at}."
    )
    return build_alert_draft(
        property_id=property_id,
        tier=rule.tier,
        alert_type=AlertType.WALK_RISK,
        title=f"Walk Risk - +{shortfall} Rooms",
        detail=detail,
        dedupe_key=walk_risk_dedupe_key(property_id, str(arrival_date)),
        source_entity_ref=_source_ref(
            change, property_id, str(arrival_date), rule_type
        ),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# UC-02: VIP Room Not Ready (CRITICAL)
# ---------------------------------------------------------------------------


@register_evaluator(AlertType.VIP_ROOM_NOT_READY)
def evaluate_vip_room_not_ready(
    change: OperationalChange, rule: RuleDefinition
) -> Optional[AlertDraft]:
    """Evaluate the VIP Room Not Ready trigger (UC-02, Requirements 4.1, 4.4).

    Fires when a VIP guest's ETA is within the configured arrival threshold and
    the assigned room status is anything other than ``Ready``. When the ETA or
    room status is missing, a CRITICAL alert is still created but flagged with
    ``incompleteInputData`` and built from whatever partial data is present
    (Requirement 4.4).

    Expected ``new_image`` attributes (``stayos-reservations`` / guest arrival):
        ``propertyId``, ``guestId``, ``etaMinutes`` (minutes until arrival),
        ``assignedRoomStatus``. Optional: ``vipTier``.

    Args:
        change: The operational change (a VIP arrival update).
        rule: The VIP Room Not Ready rule definition.

    Returns:
        A CRITICAL :class:`AlertDraft` when the trigger fires or inputs are
        incomplete, else ``None`` when inputs are complete and the room is
        ready or the ETA is outside the threshold.

    Raises:
        RuleEvaluationError: If the property or guest identifier is missing
            (these are structural, not partial-data, gaps).
    """
    image = _image(change)
    rule_type = AlertType.VIP_ROOM_NOT_READY.value
    property_id = _require(image, "propertyId", rule_type)
    guest_id = _require(image, "guestId", rule_type)

    # Record-kind guard (BUG-027): every enabled rule is evaluated against every
    # operational change, and the stayos-guests sort-key attribute is also named
    # ``guestId`` -- so a Complaint or VIP Check-In guests row carries a
    # ``guestId`` and would otherwise match here and, lacking ETA/room status,
    # produce a spurious "incomplete data" VIP Room alert. The stream record's
    # source table is not always resolvable (it can arrive as "unknown"), so the
    # guard is by record-identifying ATTRIBUTES, not the table: a Complaint row
    # carries ``complaintId``/``complaintEscalationFlag`` and a VIP Check-In row
    # carries ``stayId`` -- none of which a genuine VIP arrival (including a
    # Requirement 4.4 incomplete arrival) carries. Skip those records.
    if (
        image.get("complaintId") is not None
        or image.get("complaintEscalationFlag") is not None
        or image.get("stayId") is not None
    ):
        return None

    threshold = int(
        rule.parameters.get("arrivalThresholdMin", DEFAULT_ARRIVAL_THRESHOLD_MIN)
    )
    eta_raw = image.get("etaMinutes")
    room_status = image.get("assignedRoomStatus")

    dedupe_key = vip_room_dedupe_key(property_id, str(guest_id))
    source_ref = _source_ref(change, property_id, str(guest_id), rule_type)

    # Requirement 4.4: if ETA or room status is unavailable, still create a
    # CRITICAL alert, flag it incomplete, and retain the partial data.
    if eta_raw is None or room_status is None:
        created_at = utc_now_iso()
        known = []
        if eta_raw is not None:
            known.append(f"ETA {int(eta_raw)} min")
        if room_status is not None:
            known.append(f"room status {room_status}")
        partial = "; ".join(known) if known else "no ETA or room status available"
        return build_alert_draft(
            property_id=property_id,
            tier=rule.tier,
            alert_type=AlertType.VIP_ROOM_NOT_READY,
            title=f"VIP Room Not Ready - Guest {guest_id} (incomplete data)",
            detail=(
                f"Property {property_id}, VIP guest {guest_id}: incomplete input "
                f"data ({partial}). Created {created_at}."
            ),
            dedupe_key=dedupe_key,
            source_entity_ref=source_ref,
            created_at=created_at,
            incomplete_input_data=True,
        )

    eta_minutes = _as_int(eta_raw)
    within_threshold = eta_minutes <= threshold
    room_not_ready = room_status != ROOM_STATUS_READY
    if not (within_threshold and room_not_ready):
        return None

    created_at = utc_now_iso()
    detail = (
        f"Property {property_id}, VIP guest {guest_id}: ETA {eta_minutes} min "
        f"(threshold {threshold} min), assigned room status {room_status}. "
        f"Created {created_at}."
    )
    return build_alert_draft(
        property_id=property_id,
        tier=rule.tier,
        alert_type=AlertType.VIP_ROOM_NOT_READY,
        title=f"VIP Room Not Ready - Guest {guest_id}",
        detail=detail,
        dedupe_key=dedupe_key,
        source_entity_ref=source_ref,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# UC-04: Guest Complaint Escalation (CRITICAL)
# ---------------------------------------------------------------------------


@register_evaluator(AlertType.COMPLAINT_ESCALATION)
def evaluate_complaint_escalation(
    change: OperationalChange, rule: RuleDefinition
) -> Optional[AlertDraft]:
    """Evaluate the Guest Complaint Escalation trigger (UC-04, Requirement 5.1).

    Fires when a guest complaint escalation flag is raised through SPOG for a
    property.

    Expected ``new_image`` attributes (SPOG-backed complaint item):
        ``propertyId``, ``complaintId``, ``complaintEscalationFlag`` (bool).

    Args:
        change: The operational change (a complaint update).
        rule: The Complaint Escalation rule definition.

    Returns:
        A CRITICAL :class:`AlertDraft` when the escalation flag is set, else
        ``None``.

    Raises:
        RuleEvaluationError: If the property or complaint identifier is missing.
    """
    image = _image(change)
    rule_type = AlertType.COMPLAINT_ESCALATION.value
    property_id = _require(image, "propertyId", rule_type)
    complaint_id = _require(image, "complaintId", rule_type)

    context = {"complaint.escalationFlag": bool(image.get("complaintEscalationFlag"))}
    if not evaluate_condition(rule.trigger_condition, context):
        return None

    created_at = utc_now_iso()
    detail = (
        f"Property {property_id}: guest complaint {complaint_id} escalation flag "
        f"raised through SPOG. Created {created_at}."
    )
    return build_alert_draft(
        property_id=property_id,
        tier=rule.tier,
        alert_type=AlertType.COMPLAINT_ESCALATION,
        title=f"Guest Complaint Escalation - {complaint_id}",
        detail=detail,
        dedupe_key=complaint_dedupe_key(property_id, str(complaint_id)),
        source_entity_ref=_source_ref(
            change, property_id, str(complaint_id), rule_type
        ),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# UC-03: OOO Cluster and Group Block Impact (WARNING)
# ---------------------------------------------------------------------------


@register_evaluator(AlertType.OOO_CLUSTER)
def evaluate_ooo_cluster(
    change: OperationalChange, rule: RuleDefinition
) -> Optional[AlertDraft]:
    """Evaluate the OOO Cluster trigger (UC-03, Requirement 7.1).

    Fires when a cluster of ``minClusterSize`` (default 3) or more out-of-order
    rooms has an out-of-order date range overlapping by one or more nights with
    a group block's stay range for the same property.

    Expected ``new_image`` attributes (property OOO/blocks snapshot):
        ``propertyId``; ``oooRooms``: list of
        ``{roomId, roomType, startDate, endDate}``; ``groupBlocks``: list of
        ``{blockId, roomType, startDate, endDate}``.

    Args:
        change: The operational change (an OOO/blocks snapshot update).
        rule: The OOO Cluster rule definition.

    Returns:
        A WARNING :class:`AlertDraft` for the first group block that has an
        overlapping cluster of the required size, else ``None``.

    Raises:
        RuleEvaluationError: If the property identifier is missing or a room /
            block date is unparseable.
    """
    image = _image(change)
    rule_type = AlertType.OOO_CLUSTER.value
    property_id = _require(image, "propertyId", rule_type)

    min_cluster = int(rule.parameters.get("minClusterSize", DEFAULT_MIN_CLUSTER_SIZE))
    ooo_rooms = image.get("oooRooms") or []
    group_blocks = image.get("groupBlocks") or []

    for block in group_blocks:
        block_start = _parse_iso_date(block.get("startDate"))
        block_end = _parse_iso_date(block.get("endDate"))
        overlapping = [
            room
            for room in ooo_rooms
            if ranges_overlap_nights(
                _parse_iso_date(room.get("startDate")),
                _parse_iso_date(room.get("endDate")),
                block_start,
                block_end,
            )
        ]
        if len(overlapping) >= min_cluster:
            block_id = block.get("blockId", "unknown-block")
            created_at = utc_now_iso()
            detail = (
                f"Property {property_id}: {len(overlapping)} out-of-order rooms "
                f"overlap group block {block_id} "
                f"({block_start.isoformat()} to {block_end.isoformat()}). "
                f"Created {created_at}."
            )
            return build_alert_draft(
                property_id=property_id,
                tier=rule.tier,
                alert_type=AlertType.OOO_CLUSTER,
                title=f"OOO Cluster - {len(overlapping)} Rooms vs Block {block_id}",
                detail=detail,
                dedupe_key=ooo_cluster_dedupe_key(property_id, str(block_id)),
                source_entity_ref=_source_ref(
                    change, property_id, str(block_id), rule_type
                ),
                created_at=created_at,
            )
    return None


# ---------------------------------------------------------------------------
# UC-05: Premium Cancellation (INFO)
# ---------------------------------------------------------------------------


@register_evaluator(AlertType.PREMIUM_CANCELLATION)
def evaluate_premium_cancellation(
    change: OperationalChange, rule: RuleDefinition
) -> Optional[AlertDraft]:
    """Evaluate the Premium Cancellation trigger (UC-05, Requirements 8.1-8.3).

    Fires (INFO, no triage) when a reservation classified as premium is
    cancelled. A non-premium cancellation produces no alert (Requirement 8.2).

    Expected ``new_image`` attributes (``stayos-reservations`` item):
        ``propertyId``, ``reservationId``, ``reservationStatus``,
        ``isPremium`` (bool).

    Args:
        change: The operational change (a reservation cancellation).
        rule: The Premium Cancellation rule definition.

    Returns:
        An INFO :class:`AlertDraft` when a premium reservation is cancelled,
        else ``None``.

    Raises:
        RuleEvaluationError: If the property or reservation identifier is
            missing.
    """
    image = _image(change)
    rule_type = AlertType.PREMIUM_CANCELLATION.value
    property_id = _require(image, "propertyId", rule_type)
    reservation_id = _require(image, "reservationId", rule_type)

    status = image.get("reservationStatus")
    is_cancelled = change.event_name == "REMOVE" or status == "Cancelled"
    is_premium = bool(image.get("isPremium"))

    # Requirement 8.2: a non-premium cancellation creates no alert.
    if not (is_cancelled and is_premium):
        return None

    created_at = utc_now_iso()
    detail = (
        f"Property {property_id}: premium reservation {reservation_id} cancelled. "
        f"Consider a rate action to resell the room. Created {created_at}."
    )
    return build_alert_draft(
        property_id=property_id,
        tier=rule.tier,
        alert_type=AlertType.PREMIUM_CANCELLATION,
        title=f"Premium Cancellation - {reservation_id}",
        detail=detail,
        dedupe_key=premium_cancellation_dedupe_key(property_id, str(reservation_id)),
        source_entity_ref=_source_ref(
            change, property_id, str(reservation_id), rule_type
        ),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# UC-06: VIP Check-In Confirmation (INFO)
# ---------------------------------------------------------------------------

# Fields that identify a VIP check-in record; absence means the record is
# invalid and the alert is suppressed (Requirement 9.5).
_VIP_CHECKIN_REQUIRED_FIELDS = ("propertyId", "guestId", "stayId")


@register_evaluator(AlertType.VIP_CHECKIN)
def evaluate_vip_checkin(
    change: OperationalChange, rule: RuleDefinition
) -> Optional[AlertDraft]:
    """Evaluate the VIP Check-In Confirmation trigger (UC-06, Requirement 9.1).

    Creates an INFO alert (no triage) when a valid VIP guest check-in is
    recorded. A record missing any identifying field is suppressed and logged
    (Requirement 9.5). Duplicate check-ins for the same guest and stay collapse
    to one alert via the dedupe key (Requirement 9.4, enforced at persist).

    Expected ``new_image`` attributes (``stayos-guests`` check-in record):
        ``propertyId``, ``guestId``, ``stayId``. Optional: ``vipTier``,
        ``isVip``.

    Args:
        change: The operational change (a VIP check-in record).
        rule: The VIP Check-In rule definition.

    Returns:
        An INFO :class:`AlertDraft` for a valid VIP check-in, else ``None``
        (invalid record, or a record explicitly marked non-VIP).
    """
    image = _image(change)
    rule_type = AlertType.VIP_CHECKIN.value

    # Requirement 9.5: an invalid record (missing identifying fields) is
    # suppressed and logged, not treated as an un-evaluable rule error.
    missing = [f for f in _VIP_CHECKIN_REQUIRED_FIELDS if not image.get(f)]
    if missing:
        logger.error(
            "Suppressing VIP check-in alert: invalid record missing fields",
            extra={
                "ruleType": rule_type,
                "table": change.table,
                "missingFields": missing,
            },
        )
        return None

    # A record explicitly flagged non-VIP produces no VIP check-in alert.
    if image.get("isVip") is False:
        return None

    property_id = image["propertyId"]
    guest_id = image["guestId"]
    stay_id = image["stayId"]
    created_at = utc_now_iso()
    detail = (
        f"Property {property_id}: VIP guest {guest_id} checked in "
        f"(stay {stay_id}). Created {created_at}."
    )
    return build_alert_draft(
        property_id=property_id,
        tier=rule.tier,
        alert_type=AlertType.VIP_CHECKIN,
        title=f"VIP Check-In - Guest {guest_id}",
        detail=detail,
        dedupe_key=vip_checkin_dedupe_key(property_id, str(guest_id), str(stay_id)),
        source_entity_ref=_source_ref(change, property_id, str(guest_id), rule_type),
        created_at=created_at,
    )


__all__ = [
    "ROOM_STATUS_READY",
    "DEFAULT_ARRIVAL_THRESHOLD_MIN",
    "DEFAULT_MIN_CLUSTER_SIZE",
    "evaluate_walk_risk",
    "evaluate_vip_room_not_ready",
    "evaluate_complaint_escalation",
    "evaluate_ooo_cluster",
    "evaluate_premium_cancellation",
    "evaluate_vip_checkin",
]
