"""Property test for the pure Forecast Engine walk-plan / overflow remediation facts.

Covers the deterministic remediation-fact surface of the Forecast Engine
(``forecast.engine.run_forecast`` + ``fix_overflow_facts``): every emitted
oversell prediction carries its matching deterministic walk-plan facts -- a
``rooms_oversold`` count of at least one, a valid ISO ``anticipated_date`` inside
the run horizon, and an integer ``confidence`` -- and, when sister-property
availability is present, a deterministic overflow candidate (the sister with the
most available rooms, ties broken by the lexicographically smallest id) with its
matching available-room count. Because the engine is pure (no I/O, no clients, no
model), this is exercised directly with Hypothesis over generated per-date
demands, a horizon, and a tuple of sister-availability pairs.

Validates: Requirements 3.7.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import (
    DailyDemand,
    ForecastHorizon,
    OversellPrediction,
    build_forecast_horizon,
    fix_overflow_facts,
    run_forecast,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)


def _best_candidate(
    sister_availability: tuple[tuple[str, int], ...]
) -> tuple[str, int] | None:
    """Return the deterministic overflow candidate, or ``None`` when unusable.

    Mirrors ``fix_overflow_facts`` selection: keep only sisters with more than
    zero available rooms, then pick the one with the most rooms, breaking ties on
    the lexicographically smallest sister id.

    Args:
        sister_availability: The ``(sister_property_id, available_rooms)`` pairs.

    Returns:
        The chosen ``(sister_id, rooms)`` pair, or ``None`` when no pair has
        positive availability.
    """
    usable = [(sid, rooms) for sid, rooms in sister_availability if rooms > 0]
    if not usable:
        return None
    return sorted(usable, key=lambda pair: (-pair[1], pair[0]))[0]


# A strategy for one date's forward-looking demand. Demand (occupied + arriving)
# frequently exceeds sellable capacity so most examples surface real oversells,
# and every date carries sufficient data so it is always evaluable.
_daily_demand = st.builds(
    lambda occupied, arriving, capacity: {
        "occupied_rooms": occupied,
        "arriving_rooms": arriving,
        "sellable_capacity": capacity,
    },
    occupied=st.integers(min_value=0, max_value=200),
    arriving=st.integers(min_value=0, max_value=200),
    capacity=st.integers(min_value=0, max_value=200),
)

# A strategy for sister-property availability pairs. Room counts span negatives,
# zero, and positives so the "no usable availability" branch is also exercised;
# ids collide across pairs so the tie-break-on-smallest-id path is reachable.
_sister_availability = st.lists(
    st.tuples(
        st.sampled_from(["SIS-A", "SIS-B", "SIS-C", "SIS-D"]),
        st.integers(min_value=-5, max_value=50),
    ),
    max_size=6,
).map(tuple)


# ---------------------------------------------------------------------------
# Property 13: Every oversell carries a matching walk-plan recommendation
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 13: Every oversell carries a matching walk-plan recommendation
@PROPERTY_SETTINGS
@given(
    reference_date=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)),
    horizon_days=st.integers(min_value=1, max_value=14),
    raw_demands=st.lists(_daily_demand, min_size=1, max_size=14),
    sister_availability=_sister_availability,
    property_id=st.sampled_from(["ALOHA-CHI-001", "ALOHA-MIA-001", "ALOHA-TYO-001"]),
)
def test_property_13_oversell_carries_walkplan_facts(
    reference_date: date,
    horizon_days: int,
    raw_demands: list[dict[str, int]],
    sister_availability: tuple[tuple[str, int], ...],
    property_id: str,
) -> None:
    """Every emitted oversell carries its deterministic walk-plan facts.

    Asserts, for any generated demands over a horizon plus any generated tuple of
    ``(sister_property_id, available_rooms)`` pairs:
    - every emitted prediction has ``rooms_oversold >= 1`` and a valid ISO
      ``anticipated_date`` inside the run horizon, with an integer ``confidence``
      -- the matching deterministic walk-plan facts;
    - when at least one sister pair has ``available_rooms > 0``, every prediction's
      ``overflow_sister_property_id`` is the deterministic best candidate (most
      rooms, ties -> smallest id) and ``overflow_available_rooms`` is that
      sister's room count;
    - when no sister pair is usable (> 0), both overflow fields are ``None``;
    - ``fix_overflow_facts`` is deterministic: two calls on the same inputs yield
      identical overflow fields.
    """
    horizon = build_forecast_horizon(reference_date, horizon_days)

    # Sanity: the fixture-independent horizon builder returns the expected shape.
    assert isinstance(horizon, ForecastHorizon)
    start = date.fromisoformat(horizon.start_date)
    end = date.fromisoformat(horizon.end_date)

    # Place each generated demand on a distinct in-horizon date so the recency
    # anchor and the "date within horizon" assertions are meaningful. Dates cycle
    # across the inclusive [start, end] window.
    span_days = (end - start).days
    daily_demands = tuple(
        DailyDemand(
            date=(start + timedelta(days=index % (span_days + 1))).isoformat(),
            occupied_rooms=raw["occupied_rooms"],
            arriving_rooms=raw["arriving_rooms"],
            sellable_capacity=raw["sellable_capacity"],
            has_sufficient_data=True,
        )
        for index, raw in enumerate(raw_demands)
    )

    result = run_forecast(
        daily_demands=daily_demands,
        horizon=horizon,
        property_id=property_id,
        min_data_points=0,
        sister_availability=sister_availability,
    )

    expected_candidate = _best_candidate(sister_availability)

    for prediction in result.predictions:
        assert isinstance(prediction, OversellPrediction)

        # Matching walk-plan facts: an oversold count of at least one room ...
        assert prediction.rooms_oversold >= 1

        # ... a valid ISO anticipated_date that falls inside the run horizon ...
        anticipated = date.fromisoformat(prediction.anticipated_date)
        assert prediction.anticipated_date == anticipated.isoformat()
        assert start <= anticipated <= end

        # ... and an integer confidence in the documented [0, 100] range.
        assert isinstance(prediction.confidence, int)
        assert 0 <= prediction.confidence <= 100

        if expected_candidate is None:
            # No usable sister availability -> overflow facts left unset.
            assert prediction.overflow_sister_property_id is None
            assert prediction.overflow_available_rooms is None
        else:
            # Deterministic best candidate stamped as the overflow walk target.
            best_id, best_rooms = expected_candidate
            assert prediction.overflow_sister_property_id == best_id
            assert prediction.overflow_available_rooms == best_rooms

        # fix_overflow_facts is deterministic: re-running on a fresh prediction
        # with the same sister availability yields identical overflow fields.
        base = OversellPrediction(
            property_id=prediction.property_id,
            anticipated_date=prediction.anticipated_date,
            condition_type=prediction.condition_type,
            rooms_oversold=prediction.rooms_oversold,
            confidence=prediction.confidence,
            based_on_partial_data=prediction.based_on_partial_data,
        )
        first = fix_overflow_facts(base, sister_availability)
        second = fix_overflow_facts(base, sister_availability)
        assert (
            first.overflow_sister_property_id == second.overflow_sister_property_id
        )
        assert first.overflow_available_rooms == second.overflow_available_rooms
