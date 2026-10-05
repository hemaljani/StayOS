"""Property test for the pure Forecast Engine actionable-confidence filter.

Covers the actionable filter of the deterministic Forecast Engine
(``forecast.engine.run_forecast``): a classified oversell is emitted into
``ForecastResult.predictions`` only when its integer confidence meets the
inclusive ``actionable_confidence`` floor (default
``ACTIONABLE_CONFIDENCE_THRESHOLD`` == 50, Req 3.5). Because the engine is pure
(no I/O, no clients, no model), this is exercised directly with Hypothesis over
generated per-date demands across a horizon and a varying actionable floor.

The test is oracle-based: it independently re-derives the expected emitted set
by running ``classify_oversells`` and recomputing each oversold date's
confidence via ``compute_confidence``, then asserts ``run_forecast`` emits
exactly the oversold dates whose recomputed confidence clears the floor. This
avoids trusting the engine to filter correctly by re-implementing the filter
from its own public scoring primitives.

Validates: Requirements 3.5.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import (
    ACTIONABLE_CONFIDENCE_THRESHOLD,
    DailyDemand,
    ForecastResult,
    build_forecast_horizon,
    classify_oversells,
    compute_confidence,
    run_forecast,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# A fixed reference date anchors the generated horizon so recency scoring (which
# depends on the day-offset from the horizon start) is deterministic per example.
_REFERENCE_DATE = date(2025, 1, 1)

# The single bound run property stamped on every generated prediction.
_PROPERTY_ID = "ALOHA-TEST-001"

# Data sufficiency guard: dates carry has_sufficient_data explicitly, and a
# non-positive min_data_points defers entirely to that flag (see engine docs).
_MIN_DATA_POINTS = 0


def _daily_demand_strategy(demand_date: str) -> st.SearchStrategy[DailyDemand]:
    """Build a strategy for one date's demand spanning oversold and safe cases.

    Ranges are chosen so occupied + arriving can fall below, at, and above
    sellable capacity, exercising both non-oversold dates and oversells of
    varying shortfall magnitude (which drives the confidence score).
    """
    return st.builds(
        DailyDemand,
        date=st.just(demand_date),
        occupied_rooms=st.integers(min_value=0, max_value=120),
        arriving_rooms=st.integers(min_value=0, max_value=120),
        sellable_capacity=st.integers(min_value=1, max_value=100),
        has_sufficient_data=st.booleans(),
    )


@st.composite
def _demands_over_horizon(draw: st.DrawFn) -> tuple[list[DailyDemand], int]:
    """Draw a list of per-date demands over a bounded horizon and a floor.

    Returns the generated demands (one per consecutive date starting at the
    reference date) alongside a varied ``actionable_confidence`` floor that
    spans below, at, and above the default threshold so the inclusive boundary
    is exercised.
    """
    horizon_length = draw(st.integers(min_value=1, max_value=14))
    demands: list[DailyDemand] = []
    for offset in range(horizon_length):
        demand_date = (_REFERENCE_DATE + timedelta(days=offset)).isoformat()
        demands.append(draw(_daily_demand_strategy(demand_date)))

    # Vary the floor across [0, 100] so predictions land above, at, and below it,
    # and so the default threshold (50) is included among the drawn values.
    actionable_confidence = draw(st.integers(min_value=0, max_value=100))
    return demands, actionable_confidence


# ---------------------------------------------------------------------------
# Property 12: Actionable inclusion at confidence >= 50
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 12: Actionable inclusion at confidence >= 50
@PROPERTY_SETTINGS
@given(_demands_over_horizon())
def test_property_12_actionable_inclusion_at_confidence_threshold(
    demands_and_floor: tuple[list[DailyDemand], int],
) -> None:
    """Only oversells whose confidence clears the actionable floor are emitted.

    Asserts, for any demands over a horizon and any actionable floor:
    - every emitted prediction has ``confidence >= actionable_confidence``;
    - no oversold-but-below-floor prediction leaks in: the set of emitted dates
      equals exactly the set of independently-classified oversold dates whose
      recomputed confidence clears the floor (an inclusion oracle built from
      ``classify_oversells`` + ``compute_confidence``);
    - the inclusive boundary holds: a prediction with confidence exactly equal
      to the floor is INCLUDED.
    """
    demands, actionable_confidence = demands_and_floor
    daily_demands = tuple(demands)
    horizon = build_forecast_horizon(_REFERENCE_DATE, len(demands))

    result = run_forecast(
        daily_demands,
        horizon,
        _PROPERTY_ID,
        _MIN_DATA_POINTS,
        actionable_confidence=actionable_confidence,
    )
    assert isinstance(result, ForecastResult)

    # Every emitted prediction meets the inclusive confidence floor.
    for prediction in result.predictions:
        assert prediction.confidence >= actionable_confidence

    # Independent oracle: recompute the oversold dates that should be emitted by
    # scoring each classified oversell with the engine's own confidence function
    # and applying the same inclusive floor.
    classified, _unevaluable = classify_oversells(
        daily_demands, _PROPERTY_ID, _MIN_DATA_POINTS
    )
    expected_dates = {
        prediction.anticipated_date
        for prediction in classified
        if compute_confidence(prediction, horizon) >= actionable_confidence
    }

    emitted_dates = {prediction.anticipated_date for prediction in result.predictions}

    # No below-floor oversell leaks in and no qualifying oversell is dropped:
    # the emitted set equals exactly the oracle's expected set.
    assert emitted_dates == expected_dates

    # Boundary check: any emitted prediction whose recomputed confidence equals
    # the floor exactly is present (>= is inclusive), and construct one directly.
    for prediction in classified:
        recomputed = compute_confidence(prediction, horizon)
        if recomputed == actionable_confidence:
            assert prediction.anticipated_date in emitted_dates
