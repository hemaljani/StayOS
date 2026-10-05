"""Property test for the pure Forecast Engine confidence range.

Covers the confidence-scoring step of the deterministic Forecast Engine
(``forecast.engine.compute_confidence`` and the confidences emitted by
``forecast.engine.run_forecast``): the confidence attached to every prediction
is always an integer clamped to the inclusive [0, 100] range, regardless of
shortfall magnitude, partial-data penalty, or the recency day-offset of the
anticipated date from the horizon start. Because the engine is pure (no I/O, no
clients, no model), this is exercised directly with Hypothesis over
wide-ranging predictions and generated demand sets.

Validates: Requirement 3.4.
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
    compute_confidence,
    run_forecast,
)

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)

# Base ISO date the generated anticipated_date / demand-date offsets are derived
# from, so every generated date string is a valid ISO YYYY-MM-DD.
BASE_DATE = date(2025, 1, 1)


def _iso_from_offset(day_offset: int) -> str:
    """Return an ISO ``YYYY-MM-DD`` string ``day_offset`` days from BASE_DATE."""
    return (BASE_DATE + timedelta(days=day_offset)).isoformat()


# ---------------------------------------------------------------------------
# Property 11: Confidence is always an integer in [0, 100]
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 11: Confidence is always an integer in [0, 100]
@PROPERTY_SETTINGS
@given(
    # Wide-ranging shortfall: zero, small, and large room counts.
    rooms_oversold=st.integers(min_value=0, max_value=5000),
    based_on_partial_data=st.booleans(),
    # anticipated_date offsets across and beyond the horizon, and before it.
    anticipated_offset=st.integers(min_value=-30, max_value=60),
    horizon_days=st.integers(min_value=1, max_value=14),
)
def test_property_11_compute_confidence_integer_in_range(
    rooms_oversold: int,
    based_on_partial_data: bool,
    anticipated_offset: int,
    horizon_days: int,
) -> None:
    """compute_confidence returns an int in [0, 100] for ALL generated inputs.

    Asserts, for any shortfall magnitude, either partial-data value, and any
    anticipated-date offset (before, across, or beyond the horizon):
    - the result is a real ``int`` (and not a ``bool``, which subclasses int);
    - the result satisfies ``0 <= result <= 100``.
    """
    horizon = build_forecast_horizon(BASE_DATE, horizon_days)
    prediction = OversellPrediction(
        property_id="ALOHA-TEST-001",
        anticipated_date=_iso_from_offset(anticipated_offset),
        condition_type="OVERSELL",
        rooms_oversold=rooms_oversold,
        confidence=0,
        based_on_partial_data=based_on_partial_data,
    )

    result = compute_confidence(prediction, horizon)

    # A real int, not a bool (bool is an int subclass and must be rejected).
    assert isinstance(result, int)
    assert not isinstance(result, bool)
    assert 0 <= result <= 100


# ---------------------------------------------------------------------------
# Property 11 (via run_forecast): every emitted prediction confidence in range
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 11: Confidence is always an integer in [0, 100]
@PROPERTY_SETTINGS
@given(
    daily_demands=st.lists(
        st.builds(
            DailyDemand,
            # Derive the date from an integer offset so it is always valid ISO.
            date=st.integers(min_value=-5, max_value=20).map(_iso_from_offset),
            occupied_rooms=st.integers(min_value=0, max_value=5000),
            arriving_rooms=st.integers(min_value=0, max_value=5000),
            sellable_capacity=st.integers(min_value=0, max_value=5000),
            has_sufficient_data=st.booleans(),
        ),
        max_size=12,
    ),
    horizon_days=st.integers(min_value=1, max_value=14),
    min_data_points=st.integers(min_value=0, max_value=5),
    actionable_confidence=st.integers(min_value=0, max_value=100),
)
def test_property_11_run_forecast_confidence_in_range(
    daily_demands: list[DailyDemand],
    horizon_days: int,
    min_data_points: int,
    actionable_confidence: int,
) -> None:
    """Every prediction emitted by run_forecast has an int confidence in [0, 100].

    Runs the pure forecast end to end over generated demand sets (mixing
    over/under capacity, zero and large room counts, both sufficiency flags, and
    dates across and beyond the horizon) and asserts that every emitted
    prediction carries a real ``int`` confidence within the [0, 100] bounds.
    """
    horizon = build_forecast_horizon(BASE_DATE, horizon_days)

    result = run_forecast(
        tuple(daily_demands),
        horizon,
        property_id="ALOHA-TEST-001",
        min_data_points=min_data_points,
        actionable_confidence=actionable_confidence,
    )

    for prediction in result.predictions:
        assert isinstance(prediction.confidence, int)
        assert not isinstance(prediction.confidence, bool)
        assert 0 <= prediction.confidence <= 100
