"""Property and unit tests for the per-alert-type rule evaluators.

Covers Walk Risk trigger and exact shortfall (Property 5), VIP Room Not Ready
trigger (Property 8), OOO Cluster overlap trigger (Property 13), premium
classification (Property 15), and VIP check-in creation (Property 17); plus
unit tests for the VIP incomplete-input flag (Requirement 4.4) and invalid VIP
check-in suppression (Requirement 9.5).
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.common.models import AlertTier, AlertType
from pulse.rule_engine.evaluators import (
    ROOM_STATUS_READY,
    evaluate_ooo_cluster,
    evaluate_premium_cancellation,
    evaluate_vip_checkin,
    evaluate_vip_room_not_ready,
    evaluate_walk_risk,
)
from tests.rule_engine.conftest import make_change, make_rule

PROPERTY_SETTINGS = settings(max_examples=100)

_WALK_TRIGGER = {
    "operator": "gt",
    "left": "reservations.confirmed",
    "right": "rooms.available",
}
_PREMIUM_TRIGGER = {"operator": "eq", "left": "reservation.isPremium", "right": True}
_ROOM_STATUSES = [
    "Ready",
    "Dirty",
    "Cleaning In Progress",
    "Inspection Pending",
    "Out Of Service",
]


# ---------------------------------------------------------------------------
# Property 5: Walk Risk triggers exactly on shortfall, shortfall exact
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 5: Walk Risk triggers exactly on
# shortfall, and shortfall is exact
@PROPERTY_SETTINGS
@given(
    confirmed=st.integers(min_value=0, max_value=5000),
    available=st.integers(min_value=0, max_value=5000),
)
def test_property_5_walk_risk_trigger_and_shortfall(
    confirmed: int, available: int
) -> None:
    """A Walk Risk alert fires iff confirmed > available; shortfall is exact.

    Validates: Requirements 3.1, 3.2
    """
    rule = make_rule(AlertType.WALK_RISK, AlertTier.CRITICAL, _WALK_TRIGGER)
    change = make_change(
        "stayos-reservations",
        {
            "propertyId": "ALOHA-CHI-001",
            "arrivalDate": "2026-08-17",
            "confirmedReservations": confirmed,
            "availableRooms": available,
        },
    )

    draft = evaluate_walk_risk(change, rule)

    if confirmed > available:
        assert draft is not None
        assert draft.tier is AlertTier.CRITICAL
        shortfall = confirmed - available
        # Detail carries the exact counts and shortfall (Requirement 3.2).
        assert f"shortfall {shortfall}" in draft.detail
        assert f"{confirmed} confirmed" in draft.detail
        assert f"{available} available" in draft.detail
    else:
        assert draft is None


# ---------------------------------------------------------------------------
# Property 8: VIP Room Not Ready trigger condition
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 8: VIP Room Not Ready triggers on
# ETA-within-threshold and non-Ready room
@PROPERTY_SETTINGS
@given(
    eta_minutes=st.integers(min_value=0, max_value=480),
    room_status=st.sampled_from(_ROOM_STATUSES),
    threshold=st.integers(min_value=15, max_value=240),
)
def test_property_8_vip_room_not_ready_trigger(
    eta_minutes: int, room_status: str, threshold: int
) -> None:
    """Fires iff ETA within threshold AND room status is not Ready.

    Validates: Requirements 4.1
    """
    rule = make_rule(
        AlertType.VIP_ROOM_NOT_READY,
        AlertTier.CRITICAL,
        parameters={"arrivalThresholdMin": threshold},
    )
    change = make_change(
        "stayos-reservations",
        {
            "propertyId": "ALOHA-CHI-001",
            "guestId": "G-1",
            "etaMinutes": eta_minutes,
            "assignedRoomStatus": room_status,
        },
    )

    draft = evaluate_vip_room_not_ready(change, rule)
    should_fire = eta_minutes <= threshold and room_status != ROOM_STATUS_READY

    if should_fire:
        assert draft is not None
        assert draft.tier is AlertTier.CRITICAL
        assert draft.incomplete_input_data is False
    else:
        assert draft is None


# ---------------------------------------------------------------------------
# Property 13: OOO Cluster overlap trigger
# ---------------------------------------------------------------------------


def _iso(day_offset: int) -> str:
    """Return an ISO date string ``day_offset`` days from a fixed base."""
    return (date(2026, 8, 1) + timedelta(days=day_offset)).isoformat()


def _room(start: int, end: int, idx: int) -> dict[str, object]:
    """Build an OOO room dict spanning ``[start, end)`` day offsets."""
    return {
        "roomId": f"R-{idx}",
        "roomType": "KING",
        "startDate": _iso(start),
        "endDate": _iso(end),
    }


# Feature: initial-pulse-project, Property 13: OOO Cluster triggers on a 3+ room
# cluster overlapping a group block
@PROPERTY_SETTINGS
@given(
    block_start=st.integers(min_value=0, max_value=20),
    block_len=st.integers(min_value=1, max_value=10),
    rooms=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=30),
            st.integers(min_value=1, max_value=10),
        ),
        min_size=0,
        max_size=8,
    ),
)
def test_property_13_ooo_cluster_overlap(
    block_start: int, block_len: int, rooms: list[tuple[int, int]]
) -> None:
    """Fires iff 3+ OOO rooms overlap the group block by 1+ nights.

    Validates: Requirements 7.1
    """
    block_end = block_start + block_len
    room_dicts = [
        _room(start, start + length, idx) for idx, (start, length) in enumerate(rooms)
    ]

    # Independent oracle: count rooms overlapping [block_start, block_end).
    overlap_count = sum(
        1
        for start, length in rooms
        if start < block_end and block_start < start + length
    )

    rule = make_rule(
        AlertType.OOO_CLUSTER,
        AlertTier.WARNING,
        {"operator": "overlaps", "left": "ooo.range", "right": "block.range"},
        parameters={"minClusterSize": 3},
    )
    change = make_change(
        "stayos-rooms",
        {
            "propertyId": "ALOHA-CHI-001",
            "oooRooms": room_dicts,
            "groupBlocks": [
                {
                    "blockId": "B-1",
                    "roomType": "KING",
                    "startDate": _iso(block_start),
                    "endDate": _iso(block_end),
                }
            ],
        },
    )

    draft = evaluate_ooo_cluster(change, rule)

    if overlap_count >= 3:
        assert draft is not None
        assert draft.tier is AlertTier.WARNING
    else:
        assert draft is None


# ---------------------------------------------------------------------------
# Property 15: Premium classification determines INFO alert creation
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 15: Premium classification determines
# INFO alert creation
@PROPERTY_SETTINGS
@given(
    is_premium=st.booleans(),
    status=st.sampled_from(["Cancelled", "Active", "Confirmed"]),
)
def test_property_15_premium_classification(is_premium: bool, status: str) -> None:
    """A Premium Cancellation INFO alert is created iff cancelled AND premium.

    Validates: Requirements 8.1, 8.2
    """
    rule = make_rule(
        AlertType.PREMIUM_CANCELLATION,
        AlertTier.INFO,
        _PREMIUM_TRIGGER,
        agent_triage_enabled=False,
    )
    change = make_change(
        "stayos-reservations",
        {
            "propertyId": "ALOHA-CHI-001",
            "reservationId": "R-1",
            "reservationStatus": status,
            "isPremium": is_premium,
        },
    )

    draft = evaluate_premium_cancellation(change, rule)

    if status == "Cancelled" and is_premium:
        assert draft is not None
        assert draft.tier is AlertTier.INFO
    else:
        assert draft is None


# ---------------------------------------------------------------------------
# Property 17: VIP check-in creates an INFO alert
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 17: VIP check-in creates an INFO
# alert
@PROPERTY_SETTINGS
@given(
    guest_id=st.text(min_size=1, max_size=20),
    stay_id=st.text(min_size=1, max_size=20),
)
def test_property_17_vip_checkin_creates_info(guest_id: str, stay_id: str) -> None:
    """A valid VIP check-in record always creates an INFO alert.

    Validates: Requirements 9.1
    """
    rule = make_rule(
        AlertType.VIP_CHECKIN,
        AlertTier.INFO,
        agent_triage_enabled=False,
    )
    change = make_change(
        "stayos-guests",
        {
            "propertyId": "ALOHA-CHI-001",
            "guestId": guest_id,
            "stayId": stay_id,
            "vipTier": "Ambassador",
        },
    )

    draft = evaluate_vip_checkin(change, rule)

    assert draft is not None
    assert draft.tier is AlertTier.INFO
    assert draft.type is AlertType.VIP_CHECKIN


# ---------------------------------------------------------------------------
# Unit test: Requirement 4.4 - VIP incomplete input flag
# ---------------------------------------------------------------------------


def test_requirement_4_4_incomplete_input_flagged() -> None:
    """Missing ETA/room status still creates a CRITICAL alert flagged incomplete."""
    rule = make_rule(AlertType.VIP_ROOM_NOT_READY, AlertTier.CRITICAL)
    # Missing both etaMinutes and assignedRoomStatus.
    change = make_change(
        "stayos-reservations",
        {"propertyId": "ALOHA-CHI-001", "guestId": "G-1"},
    )

    draft = evaluate_vip_room_not_ready(change, rule)

    assert draft is not None
    assert draft.tier is AlertTier.CRITICAL
    assert draft.incomplete_input_data is True


def test_requirement_4_4_partial_input_retained() -> None:
    """A partial record (ETA present, status missing) is retained and flagged."""
    rule = make_rule(AlertType.VIP_ROOM_NOT_READY, AlertTier.CRITICAL)
    change = make_change(
        "stayos-reservations",
        {"propertyId": "ALOHA-CHI-001", "guestId": "G-1", "etaMinutes": 30},
    )

    draft = evaluate_vip_room_not_ready(change, rule)

    assert draft is not None
    assert draft.incomplete_input_data is True
    assert "ETA 30 min" in draft.detail


def test_bug_027_vip_room_ignores_guests_table_complaint_row() -> None:
    """A complaint row on stayos-guests must NOT produce a VIP Room alert.

    Every enabled rule is evaluated against every change, and the stayos-guests
    sort-key attribute is also ``guestId`` -- so a complaint row (guestId set,
    no ETA/room status) previously produced a spurious "incomplete data" VIP
    Room alert. The record-kind guard must skip guests-table changes.
    """
    rule = make_rule(AlertType.VIP_ROOM_NOT_READY, AlertTier.CRITICAL)
    # A complaint row as written by the demo simulator to stayos-guests: the
    # guestId is the COMPLAINT#-prefixed sort key value.
    change = make_change(
        "stayos-guests",
        {
            "propertyId": "ALOHA-CHI-001",
            "guestId": "COMPLAINT#C-1001-abc",
            "complaintId": "C-1001-abc",
            "complaintEscalationFlag": True,
        },
    )

    assert evaluate_vip_room_not_ready(change, rule) is None


# ---------------------------------------------------------------------------
# Unit test: Requirement 9.5 - invalid VIP check-in suppressed
# ---------------------------------------------------------------------------


def test_requirement_9_5_invalid_checkin_suppressed() -> None:
    """A check-in record missing an identifying field creates no alert."""
    rule = make_rule(AlertType.VIP_CHECKIN, AlertTier.INFO, agent_triage_enabled=False)
    # Missing guestId (required identifying field).
    change = make_change(
        "stayos-guests",
        {"propertyId": "ALOHA-CHI-001", "stayId": "S-1"},
    )

    assert evaluate_vip_checkin(change, rule) is None


def test_requirement_9_5_missing_stay_suppressed() -> None:
    """A check-in record missing stayId creates no alert."""
    rule = make_rule(AlertType.VIP_CHECKIN, AlertTier.INFO, agent_triage_enabled=False)
    change = make_change(
        "stayos-guests",
        {"propertyId": "ALOHA-CHI-001", "guestId": "G-1"},
    )

    assert evaluate_vip_checkin(change, rule) is None
