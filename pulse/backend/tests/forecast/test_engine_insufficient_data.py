"""Property test for insufficient-data handling in the pure Forecast Engine.

Covers the data-sufficiency step of the deterministic Forecast Engine
(``forecast.engine.classify_oversells``): a horizon date whose demand carries
``has_sufficient_data=False`` is excluded from oversell classification, recorded
in the returned ``unevaluable_dates`` list (in horizon order), and never
terminates the run. Evaluable oversold dates surrounding an unevaluable one
still produce predictions, proving the run continues past insufficient dates.

Because the engine is pure (no I/O, no clients, no model), this is exercised
directly with Hypothesis over mixed lists of ``DailyDemand`` where some entries
are marked insufficient - including some that WOULD be oversold if evaluated.

Validates: Requirements 3.3, 8.2.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from forecast.engine import DailyDemand, classify_oversells

# Minimum property-based iterations mandated by the design Testing Strategy.
PROPERTY_SETTINGS = settings(max_examples=100)


def _daily_demand(day_offset: int, draw: st.DataObject) -> DailyDemand:
    """Draw a single DailyDemand for a distinct horizon date.

    The date is anchored at a fixed reference plus ``day_offset`` so every date
    in a generated list is unique and the horizon order is well defined. Demand
    figures span both oversold and not-oversold cases, and ``has_sufficient_data``
    is drawn freely so insufficient dates that WOULD be oversold occur.

    Args:
        day_offset: The whole-day offset from the reference date, giving this
            demand a distinct calendar date within the horizon.
        draw: The Hypothesis ``data()`` object used to draw the demand fields.

    Returns:
        A DailyDemand with a unique date and freely drawn sufficiency flag.
    """
    anchor = date(2025, 1, 1)
    demand_date = (anchor + timedelta(days=day_offset)).isoformat()
    occupied = draw.draw(st.integers(min_value=0, max_value=200))
    arriving = draw.draw(st.integers(min_value=0, max_value=200))
    capacity = draw.draw(st.integers(min_value=0, max_value=200))
    has_sufficient = draw.draw(st.booleans())
    return DailyDemand(
        date=demand_date,
        occupied_rooms=occupied,
        arriving_rooms=arriving,
        sellable_capacity=capacity,
        has_sufficient_data=has_sufficient,
    )


# ---------------------------------------------------------------------------
# Property 10: Dates with insufficient data are excluded, recorded, and never
# terminate the run
# ---------------------------------------------------------------------------


# Feature: predictive-forecasting-agent, Property 10: Dates with insufficient data are excluded, recorded, and never terminate the run
@PROPERTY_SETTINGS
@given(
    data=st.data(),
    # A small horizon of distinct dates; a length of 1..14 mirrors the engine's
    # horizon bounds and keeps each example a mixed multi-date list.
    length=st.integers(min_value=1, max_value=14),
    property_id=st.sampled_from(
        ["ALOHA-CHI-001", "ALOHA-MIA-001", "ALOHA-TYO-001"]
    ),
    min_data_points=st.integers(min_value=0, max_value=5),
)
def test_property_10_insufficient_data_excluded_recorded_never_terminates(
    data: st.DataObject,
    length: int,
    property_id: str,
    min_data_points: int,
) -> None:
    """Insufficient-data dates: excluded, recorded in order, never terminate.

    Asserts, for any mixed list of per-date demands:
    - every date with ``has_sufficient_data=False`` appears in
      ``unevaluable_dates``;
    - no prediction is ever produced for an insufficient-data date;
    - processing continues past insufficient dates - every evaluable oversold
      date still produces a prediction, so an unevaluable date never terminates
      the run early;
    - ``unevaluable_dates`` preserves horizon order and contains exactly the
      insufficient-data dates (no more, no fewer, no reordering).
    """
    daily_demands = tuple(
        _daily_demand(offset, data) for offset in range(length)
    )

    predictions, unevaluable_dates = classify_oversells(
        daily_demands, property_id, min_data_points
    )

    # The exact set/sequence of dates we expect to be excluded: those flagged
    # insufficient by the reader, taken in the original horizon order.
    expected_unevaluable = [
        demand.date
        for demand in daily_demands
        if not demand.has_sufficient_data
    ]

    # unevaluable_dates contains EXACTLY the insufficient dates, in horizon order.
    assert list(unevaluable_dates) == expected_unevaluable

    insufficient_dates = set(expected_unevaluable)
    predicted_dates = [prediction.anticipated_date for prediction in predictions]

    # No prediction is ever produced for an insufficient-data date.
    for predicted_date in predicted_dates:
        assert predicted_date not in insufficient_dates

    # Every date with insufficient data appears in unevaluable_dates (covered by
    # the exact-equality assertion above, restated as the direct property).
    for demand in daily_demands:
        if not demand.has_sufficient_data:
            assert demand.date in unevaluable_dates

    # Processing continues past insufficient dates: independently recompute which
    # EVALUABLE dates are oversold (occupied + arriving exceeds capacity by >= 1)
    # and assert each one still produced a prediction. If an insufficient date
    # terminated the run early, a later evaluable oversold date would be missing.
    expected_predicted_dates = [
        demand.date
        for demand in daily_demands
        if demand.has_sufficient_data
        and (demand.occupied_rooms + demand.arriving_rooms)
        - demand.sellable_capacity
        >= 1
    ]
    assert sorted(predicted_dates) == sorted(expected_predicted_dates)
