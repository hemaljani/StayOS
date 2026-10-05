"""Property and unit tests for the triage specializations.

Covers Property 6 (walk strategy respects the walkable cap and loyalty
threshold), Property 10 (complaint options well-formed), Property 14
(replacement-room options bounded, matched, suitability-ordered), and the unit
cases for no-sister-property (3.6), complaint <3 options (5.4), and no
replacement rooms (7.4).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.common.errors import TriageFailure
from pulse.common.models import ReviewRisk
from pulse.triage.context import LOYALTY_RANK, SituationContext, loyalty_rank
from pulse.triage.specializations import (
    OOO_MAX_REPLACEMENTS,
    build_complaint_options,
    build_ooo_replacement_options,
    build_walk_strategy,
    draft_group_notification,
)

PROPERTY_SETTINGS = settings(max_examples=100)

_TIERS = list(LOYALTY_RANK.keys())
_RISK_VALUES = [risk.value for risk in ReviewRisk]


def _always_sister(_dates: tuple[str, str]) -> str:
    """A sister-property lookup that always finds availability."""
    return "ALOHA-CHI-002"


# ---------------------------------------------------------------------------
# Property 6: walk strategy respects the walkable cap and loyalty threshold
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 6: Walk strategy respects the
# walkable cap and loyalty threshold
@PROPERTY_SETTINGS
@given(
    guests=st.lists(
        st.fixed_dictionaries(
            {
                "guestId": st.text(min_size=1, max_size=6),
                "loyaltyTier": st.sampled_from(_TIERS),
            }
        ),
        min_size=0,
        max_size=12,
    ),
    shortfall=st.integers(min_value=0, max_value=10),
    protection_tier=st.sampled_from(_TIERS),
)
def test_property_6_walk_strategy_cap_and_threshold(
    guests: list[dict], shortfall: int, protection_tier: str
) -> None:
    """Walkable guests are at/below threshold, capped at shortfall, 1 comp each.

    Validates: Requirements 3.3, 3.5
    """
    # Give each guest a distinct reservationId for the WalkableGuest mapping.
    for index, guest in enumerate(guests):
        guest["reservationId"] = f"R-{index}"

    context = SituationContext(
        property_id="ALOHA-CHI-001",
        confirmed_guests=guests,
        room_shortfall=shortfall,
        loyalty_protection_tier=protection_tier,
        stay_dates=("2026-08-17", "2026-08-19"),
        sister_property_lookup=_always_sister,
    )

    strategy = build_walk_strategy(context)
    protection_rank = loyalty_rank(protection_tier)

    # Never exceeds the shortfall cap.
    assert len(strategy.walkable_guests) <= shortfall
    # Every selected guest is at or below the protection threshold.
    for walkable in strategy.walkable_guests:
        assert loyalty_rank(walkable["loyalty_tier"]) <= protection_rank
    # Exactly one compensation package per selected guest.
    assert len(strategy.compensation) == len(strategy.walkable_guests)
    # Cannot select more guests than are actually eligible.
    eligible = sum(
        1 for g in guests if loyalty_rank(g["loyaltyTier"]) <= protection_rank
    )
    assert len(strategy.walkable_guests) == min(eligible, shortfall)


def test_walk_strategy_never_recommends_a_sister_property() -> None:
    """Option B: the walk strategy never assigns a cross-city sister property.

    Even if a sister-property lookup WOULD return an available property, the
    reframed strategy ignores it (the pilot estate is one hotel per city, so a
    same-brand property is in another city/country and is not a realistic walk
    target). Relocation is handled as in-house/partner-overflow in the ranked
    options instead. The walkable-guest list is still produced.

    Validates: Walk Risk Option B reframe (supersedes the old Requirement 3.6
    sister-availability behavior).
    """
    context = SituationContext(
        property_id="ALOHA-CHI-001",
        confirmed_guests=[
            {"guestId": "G-1", "reservationId": "R-1", "loyaltyTier": "Gold"}
        ],
        room_shortfall=1,
        loyalty_protection_tier="Gold",
        stay_dates=("2026-08-17", "2026-08-19"),
        # Even with a lookup that WOULD find a sister, none is recommended.
        sister_property_lookup=_always_sister,
    )

    strategy = build_walk_strategy(context)

    assert strategy.sister_property_available is False
    assert strategy.sister_property_id is None
    # A walkable guest is still identified (the in-house/partner-walk candidate).
    assert len(strategy.walkable_guests) == 1
    assert len(strategy.compensation) == 1


# ---------------------------------------------------------------------------
# Property 10: complaint triage options are well-formed
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 10: Complaint triage options are
# well-formed
@PROPERTY_SETTINGS
@given(
    candidates=st.lists(
        st.fixed_dictionaries(
            {
                "title": st.text(min_size=1, max_size=20),
                "detail": st.text(min_size=1, max_size=40),
                "estimatedCost": st.floats(
                    min_value=0.0,
                    max_value=10000.0,
                    allow_nan=False,
                    allow_infinity=False,
                ),
                "reviewRisk": st.sampled_from(_RISK_VALUES),
                "recommended": st.booleans(),
            }
        ),
        min_size=3,
        max_size=8,
    )
)
def test_property_10_complaint_options_well_formed(candidates: list[dict]) -> None:
    """3-5 options, each numeric cost + review risk, exactly one recommended.

    Validates: Requirements 5.2
    """
    options = build_complaint_options(candidates)

    assert 3 <= len(options) <= 5
    for option in options:
        assert isinstance(option.estimated_cost, float)
        assert option.review_risk in set(ReviewRisk)
    assert sum(1 for o in options if o.recommended) == 1
    # Ranks are contiguous starting at 1 and labels unique.
    assert [o.rank for o in options] == list(range(1, len(options) + 1))
    assert len({o.label for o in options}) == len(options)


def test_requirement_5_4_fewer_than_three_options_fails() -> None:
    """Fewer than 3 candidates raises a TriageFailure for manual resolution.

    Validates: Requirement 5.4
    """
    candidates = [
        {"title": "A", "detail": "d", "estimatedCost": 100.0, "reviewRisk": "Low"},
        {"title": "B", "detail": "d", "estimatedCost": 200.0, "reviewRisk": "High"},
    ]
    try:
        build_complaint_options(candidates)
    except TriageFailure as failure:
        assert failure.reason == "insufficient_options"
        return
    raise AssertionError("Expected TriageFailure for fewer than 3 options")


# ---------------------------------------------------------------------------
# Property 14: replacement-room options bounded, matched, suitability-ordered
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 14: Replacement-room options are
# bounded, matched, and suitability-ordered
@PROPERTY_SETTINGS
@given(
    rooms=st.lists(
        st.fixed_dictionaries(
            {
                "roomId": st.text(min_size=1, max_size=6),
                "roomType": st.sampled_from(["KING", "QUEEN", "SUITE"]),
                "availableForRange": st.booleans(),
                "suitability": st.floats(
                    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
                ),
            }
        ),
        min_size=0,
        max_size=12,
    ),
    required_type=st.sampled_from(["KING", "QUEEN", "SUITE"]),
)
def test_property_14_replacement_rooms_bounded_matched_ordered(
    rooms: list[dict], required_type: str
) -> None:
    """At most 5 rooms, all type-matched and available, suitability-ordered.

    Validates: Requirements 7.2
    """
    # Give each room a distinct id so ordering can be verified unambiguously.
    for index, room in enumerate(rooms):
        room["roomId"] = f"RM-{index}"

    context = SituationContext(
        property_id="ALOHA-CHI-001",
        required_room_type=required_type,
        replacement_candidates=rooms,
    )
    options = build_ooo_replacement_options(context)

    # At most 5 options (Requirement 7.2).
    assert len(options) <= OOO_MAX_REPLACEMENTS

    # Independent oracle: matched + available, sorted by suitability desc.
    matched = [
        room
        for room in rooms
        if room["roomType"] == required_type and room["availableForRange"]
    ]
    matched.sort(key=lambda r: r["suitability"], reverse=True)
    expected_ids = [
        f"Replacement room {r['roomId']}" for r in matched[:OOO_MAX_REPLACEMENTS]
    ]
    assert [o.title for o in options] == expected_ids


def test_requirement_7_4_no_replacement_rooms_zero_options() -> None:
    """When no room matches, zero options are presented and a notice drafted.

    Validates: Requirement 7.4
    """
    context = SituationContext(
        property_id="ALOHA-CHI-001",
        required_room_type="SUITE",
        replacement_candidates=[
            {
                "roomId": "RM-1",
                "roomType": "KING",
                "availableForRange": True,
                "suitability": 0.9,
            },
            {
                "roomId": "RM-2",
                "roomType": "SUITE",
                "availableForRange": False,
                "suitability": 0.8,
            },
        ],
        group_block={"blockId": "B-1"},
    )

    options = build_ooo_replacement_options(context)
    assert options == []

    notification = draft_group_notification(context, options)
    assert "B-1" in notification
